"""
Google (Calendar, Gmail, Classroom, Drive) para varias cuentas.

Las cuentas se llaman por un alias ("escuela", "personal"). Sus tokens OAuth viven en
/data/google/<alias>.json (volumen bot-data). Todas las funciones son síncronas (la
librería de Google lo es): el bot las llama con asyncio.to_thread.

Conexión de una cuenta (sin servidor web): el bot manda el link de autorización; tras
aceptar, Google redirige a http://localhost:8765/?code=..., que no carga en el iPhone.
Salvador copia esa URL de la barra del navegador y la pega en Telegram.
"""
import logging
import base64
import io
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta
import mimetypes
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SECRETS_DIR = Path(os.environ.get("GOOGLE_SECRETS_DIR", "/secrets"))
TOKENS_DIR = Path(os.environ.get("GOOGLE_TOKENS_DIR", "/data/google"))
REDIRECT_URI = "http://localhost:8765/"
TZ_NAME = os.environ.get("TZ", "America/Mexico_City")
TZ = ZoneInfo(TZ_NAME)

CUENTAS = [c.strip() for c in os.environ.get("GOOGLE_CUENTAS", "escuela,personal").split(",") if c.strip()]
CUENTA_CLASSROOM = os.environ.get("GOOGLE_CUENTA_CLASSROOM", "escuela")
CUENTA_CALENDARIO = os.environ.get("GOOGLE_CALENDARIO_PRINCIPAL", "personal")
# Cuentas que son el MISMO Gmail con otro nombre: su Gmail personal es también el de buscar trabajo, así que
# 'empleo' usa el token de 'personal' (si no, cada evento y cada correo salen dos veces).
ALIAS = {a.strip(): b.strip() for a, _, b in (x.partition("=") for x in
         os.environ.get("GOOGLE_ALIAS", "empleo=personal").split(",")) if a.strip() and b.strip()}


def real(cuenta: str) -> str:
    return ALIAS.get(cuenta, cuenta)

SCOPES_BASE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",  # marcar leído, archivar, etiquetas, borradores
]
SCOPES_CLASSROOM = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",  # adjuntos de las tareas
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",   # profesores
    "https://www.googleapis.com/auth/classroom.profile.emails",     # correo de los profesores
    "https://www.googleapis.com/auth/classroom.topics.readonly",    # temas (p. ej. "Parcial 2")
]


def scopes(cuenta: str) -> list[str]:
    return SCOPES_BASE + (SCOPES_CLASSROOM if cuenta == CUENTA_CLASSROOM else [])


def client_file(cuenta: str) -> Path:
    """Credenciales OAuth de esa cuenta: secrets/google_client_<cuenta>.json, o el genérico
    secrets/google_client.json. Por cuenta porque la escuela usa un proyecto "Interno" del
    CETI y el Gmail personal otro proyecto "Externo"."""
    propio = SECRETS_DIR / f"google_client_{cuenta}.json"
    return propio if propio.exists() else SECRETS_DIR / "google_client.json"


def configurado(cuenta: str = "") -> bool:
    if cuenta:
        return client_file(cuenta).exists()
    return any(client_file(c).exists() for c in CUENTAS)


# ---------- OAuth ----------

def iniciar_conexion(cuenta: str) -> tuple[str, Flow]:
    flow = Flow.from_client_secrets_file(str(client_file(cuenta)), scopes=scopes(cuenta), redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
    return url, flow


def terminar_conexion(cuenta: str, flow: Flow, url_pegada: str) -> str:
    qs = parse_qs(urlparse(url_pegada.strip()).query)
    if "error" in qs:
        raise RuntimeError(f"Google respondió: {qs['error'][0]}")
    code = qs.get("code", [None])[0]
    if not code:
        raise RuntimeError("Esa URL no trae el código (?code=...).")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    (TOKENS_DIR / f"{cuenta}.json").write_text(flow.credentials.to_json())
    _svc_cache.clear()
    _faltantes_cache.pop(cuenta, None)
    return email(cuenta)


def _creds(cuenta: str) -> Credentials:
    cuenta = real(cuenta)
    p = TOKENS_DIR / f"{cuenta}.json"
    if not p.exists():
        raise RuntimeError(f"La cuenta '{cuenta}' no está conectada (usa /google {cuenta}).")
    info = json.loads(p.read_text())
    # Sin "scopes": al renovar, Google devuelve los permisos que el usuario realmente concedió.
    # Si se mandan y alguno no quedó concedido, Google responde invalid_scope.
    info.pop("scopes", None)
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return creds


class _CachePorHilo(threading.local):
    """httplib2 (lo que usa la librería de Google) no es seguro entre hilos: cada hilo de
    asyncio.to_thread tiene su propio cliente. Compartirlo causaba timeouts al azar.
    clear() sube una generación global: así se invalida la caché de TODOS los hilos (p. ej.
    al reconectar una cuenta con permisos nuevos), no solo la del hilo que lo llama."""
    generacion = 0  # atributo de clase: compartido entre hilos

    def __init__(self):
        self.d = {}
        self.gen = _CachePorHilo.generacion

    def clear(self):
        _CachePorHilo.generacion += 1

    def vigente(self) -> dict:
        if self.gen != _CachePorHilo.generacion:
            self.d = {}
            self.gen = _CachePorHilo.generacion
        return self.d


_svc_cache = _CachePorHilo()


def _svc(cuenta: str, api: str, version: str):
    cuenta = real(cuenta)
    key = (cuenta, api)
    cache = _svc_cache.vigente()
    if key not in cache:
        cache[key] = build(api, version, credentials=_creds(cuenta), cache_discovery=False)
    return cache[key]


_faltantes_cache: dict[str, tuple[float, list[str]]] = {}


def permisos_faltantes_cache(cuenta: str) -> list[str]:
    """Igual que permisos_faltantes pero cacheado 10 min (se usa en cada mensaje)."""
    ts, val = _faltantes_cache.get(cuenta, (0, []))
    if time.time() - ts > 600:
        try:
            val = permisos_faltantes(cuenta)
        except Exception:
            val = []
        _faltantes_cache[cuenta] = (time.time(), val)
    return val


def permisos_faltantes_guardados(cuenta: str) -> list[str]:
    """Solo lee la caché (sin red): se puede llamar desde el event loop."""
    return _faltantes_cache.get(cuenta, (0, []))[1]


def permisos_faltantes(cuenta: str) -> list[str]:
    """Permisos pedidos que el usuario NO palomeó al conectar (Google los muestra como casillas)."""
    c = _creds(cuenta)
    if not c.valid:
        c.refresh(Request())
    info = json.load(urlopen(f"https://oauth2.googleapis.com/tokeninfo?access_token={c.token}", timeout=15))
    concedidos = set(info.get("scope", "").split())
    # Google concede coursework.me.readonly bajo su alias student-submissions.me.readonly
    if any("classroom.student-submissions.me" in sc for sc in concedidos):
        concedidos.add("https://www.googleapis.com/auth/classroom.coursework.me.readonly")
    faltan = [sc for sc in scopes(cuenta) if sc.startswith("https://www.googleapis.com/auth/")
              and sc not in concedidos and not sc.endswith("userinfo.email")]
    return [sc.rsplit("/", 1)[-1] for sc in faltan]


def conectadas() -> list[str]:
    return [c for c in CUENTAS if c not in ALIAS and (TOKENS_DIR / f"{c}.json").exists()]


def email(cuenta: str) -> str:
    return _svc(cuenta, "oauth2", "v2").userinfo().get().execute().get("email", "?")


# ---------- fechas ----------

def _parse(texto: str) -> tuple[datetime | None, str | None]:
    """'AAAA-MM-DD HH:MM' → (datetime, None); 'AAAA-MM-DD' → (None, fecha) = todo el día."""
    texto = texto.strip().replace("T", " ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        return None, texto
    return datetime.strptime(texto[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ), None


def _fmt_evento(e: dict, cuenta: str, calendario: str = "primary", nombre_cal: str = "") -> dict:
    ini = e.get("start", {})
    fin = e.get("end", {})
    d = {
        "id": e["id"], "cuenta": cuenta, "calendario_id": calendario, "calendario": nombre_cal or calendario,
        "titulo": e.get("summary", "(sin título)"),
        "inicio": (ini.get("dateTime") or ini.get("date", ""))[:16].replace("T", " "),
        "fin": (fin.get("dateTime") or fin.get("date", ""))[:16].replace("T", " "),
        "todo_el_dia": "date" in ini, "lugar": e.get("location", ""),
    }
    if e.get("recurringEventId"):
        d["serie_id"] = e["recurringEventId"]
    if e.get("hangoutLink"):
        d["meet"] = e["hangoutLink"]
    if e.get("attendees"):
        d["invitados"] = [a.get("email") for a in e["attendees"]]
    return d


# ---------- Calendar ----------

# colorId de eventos de Google Calendar (los nombres oficiales en español + alias comunes)
COLORES = {
    "lavanda": "1", "salvia": "2", "uva": "3", "flamenco": "4", "platano": "5", "plátano": "5",
    "mandarina": "6", "pavo real": "7", "grafito": "8", "arandano": "9", "arándano": "9",
    "albahaca": "10", "tomate": "11",
    "verde": "10", "verde claro": "2", "rojo": "11", "amarillo": "5", "naranja": "6", "morado": "3",
    "azul": "9", "azul claro": "7", "turquesa": "7", "rosa": "4", "gris": "8", "lila": "1",
}


def _color_id(color: str) -> str | None:
    c = (color or "").strip().lower()
    if not c:
        return None
    if c.isdigit() and 1 <= int(c) <= 11:
        return c
    return COLORES.get(c)

def _rango(desde: str, hasta: str) -> tuple[datetime, datetime]:
    d_ini, f_ini = _parse(desde)
    d_fin, f_fin = _parse(hasta)
    t_min = d_ini or datetime.fromisoformat(f_ini).replace(tzinfo=TZ)
    t_max = d_fin or (datetime.fromisoformat(f_fin).replace(tzinfo=TZ) + timedelta(days=1))
    return t_min, t_max


def cal_calendarios() -> list[dict]:
    """Todos los calendarios visibles de todas las cuentas."""
    res = []
    for c in conectadas():
        try:  # una cuenta con el token vencido no debe tumbar el calendario de las demás
            items = _svc(c, "calendar", "v3").calendarList().list().execute().get("items", [])
        except Exception as e:
            logging.getLogger(__name__).warning("calendario de %s no disponible: %s", c, e)
            continue
        for cal in items:
            if cal.get("hidden"):
                continue
            res.append({"cuenta": c, "id": "primary" if cal.get("primary") else cal["id"],
                        "nombre": cal.get("summaryOverride") or cal.get("summary", ""),
                        "color": cal.get("backgroundColor"), "rol": cal.get("accessRole")})
    return res


def cal_listar(desde: str, hasta: str) -> list[dict]:
    """Eventos de TODOS los calendarios visibles (principal, tareas, festivos, compartidos...)."""
    t_min, t_max = _rango(desde, hasta)
    eventos = []
    for cal in cal_calendarios():
        try:
            r = _svc(cal["cuenta"], "calendar", "v3").events().list(
                calendarId=cal["id"], timeMin=t_min.isoformat(), timeMax=t_max.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=100).execute()
        except Exception:
            continue
        eventos += [_fmt_evento(e, cal["cuenta"], cal["id"], cal["nombre"]) for e in r.get("items", [])]
    return sorted(eventos, key=lambda e: e["inicio"])


def cal_ocupado(desde: str, hasta: str) -> list[dict]:
    """Bloques ocupados (todas las cuentas, calendarios principales + tareas), unidos y ordenados."""
    t_min, t_max = _rango(desde, hasta)
    bloques = []
    for c in conectadas():
        ids = [{"id": cal["id"] if cal["id"] != "primary" else email(c)} for cal in cal_calendarios() if cal["cuenta"] == c]
        if not ids:
            continue
        try:
            r = _svc(c, "calendar", "v3").freebusy().query(body={
                "timeMin": t_min.isoformat(), "timeMax": t_max.isoformat(), "timeZone": TZ_NAME, "items": ids}).execute()
        except Exception as e:
            logging.getLogger(__name__).warning("ocupado de %s no disponible: %s", c, e)
            continue
        for cal in r.get("calendars", {}).values():
            bloques += [(datetime.fromisoformat(b["start"]).astimezone(TZ), datetime.fromisoformat(b["end"]).astimezone(TZ))
                        for b in cal.get("busy", [])]
    bloques.sort()
    unidos: list[list[datetime]] = []
    for a, b in bloques:
        if unidos and a <= unidos[-1][1]:
            unidos[-1][1] = max(unidos[-1][1], b)
        else:
            unidos.append([a, b])
    return [{"desde": a.strftime("%Y-%m-%d %H:%M"), "hasta": b.strftime("%Y-%m-%d %H:%M")} for a, b in unidos]


DIAS_RRULE = {"lu": "MO", "ma": "TU", "mi": "WE", "ju": "TH", "vi": "FR", "sa": "SA", "do": "SU"}


def _sin_acentos(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def _rrule(repetir: str, hasta: str = "", veces: int = 0) -> str | None:
    """'diario' | 'semanal' | 'semanal:lu,mi,vi' | 'mensual' | 'anual' | 'RRULE:...'."""
    r = (repetir or "").strip().lower()
    if not r:
        return None
    if r.upper().startswith("RRULE:"):
        return repetir.strip()
    base, _, dias = r.partition(":")
    freq = {"diario": "DAILY", "semanal": "WEEKLY", "mensual": "MONTHLY", "anual": "YEARLY"}.get(base)
    if not freq:
        raise ValueError(f"repetir no válido: {repetir}")
    regla = f"RRULE:FREQ={freq}"
    if dias:
        byday = []
        for d in dias.split(","):
            clave = _sin_acentos(d.strip())[:2]
            if not clave:
                continue
            if clave not in DIAS_RRULE:
                raise ValueError(f"día no válido en repetir: {d.strip()} (usa lu, ma, mi, ju, vi, sa, do)")
            byday.append(DIAS_RRULE[clave])
        regla += ";BYDAY=" + ",".join(byday)
    if hasta:
        # UNTIL va en UTC: fin del día LOCAL, para no perder la última repetición de eventos por la tarde.
        fin_local = datetime.fromisoformat(hasta.strip()[:10]).replace(hour=23, minute=59, second=59, tzinfo=TZ)
        regla += ";UNTIL=" + fin_local.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    elif veces:
        regla += f";COUNT={int(veces)}"
    return regla


def cal_crear(titulo: str, inicio: str, fin: str = "", duracion_min: int = 60, descripcion: str = "",
              lugar: str = "", cuenta: str = "", avisos_min: list[int] | None = None, color: str = "",
              repetir: str = "", repetir_hasta: str = "", repetir_veces: int = 0,
              invitados: list[str] | None = None, meet: bool = False, calendario_id: str = "primary") -> dict:
    cuenta = cuenta or CUENTA_CALENDARIO
    if cuenta not in conectadas() and conectadas():
        cuenta = conectadas()[0]  # el principal aún no está conectado: usa el que haya
    d_ini, f_ini = _parse(inicio)
    body = {"summary": titulo, "description": descripcion, "location": lugar}
    if cid := _color_id(color):
        body["colorId"] = cid
    if d_ini:
        d_fin = _parse(fin)[0] if fin else d_ini + timedelta(minutes=duracion_min or 60)
        body["start"] = {"dateTime": d_ini.isoformat(), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": d_fin.isoformat(), "timeZone": TZ_NAME}
    else:
        f_fin = _parse(fin)[1] if fin else None
        f_fin = f_fin or (datetime.fromisoformat(f_ini) + timedelta(days=1)).strftime("%Y-%m-%d")
        body["start"] = {"date": f_ini}
        body["end"] = {"date": f_fin}
    if avisos_min:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup", "minutes": int(m)} for m in avisos_min[:5]]}
    if regla := _rrule(repetir, repetir_hasta, repetir_veces):
        body["recurrence"] = [regla]
    if invitados:
        body["attendees"] = [{"email": x} for x in invitados]
    extra = {}
    if meet:
        body["conferenceData"] = {"createRequest": {"requestId": f"korra-{int(time.time())}",
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        extra["conferenceDataVersion"] = 1
    e = _svc(cuenta, "calendar", "v3").events().insert(
        calendarId=calendario_id or "primary", body=body, sendUpdates="all" if invitados else "none", **extra).execute()
    return _fmt_evento(e, cuenta, calendario_id) | {"link": e.get("htmlLink", "")}


def cal_editar(cuenta: str, evento_id: str, titulo: str = "", inicio: str = "", fin: str = "",
               descripcion: str | None = None, lugar: str | None = None, color: str = "",
               calendario_id: str = "primary") -> dict:
    """Para cambiar una serie completa, usa el serie_id como evento_id."""
    svc = _svc(cuenta, "calendar", "v3")
    cal = calendario_id or "primary"
    e = svc.events().get(calendarId=cal, eventId=evento_id).execute()
    if titulo:
        e["summary"] = titulo
    if cid := _color_id(color):
        e["colorId"] = cid
    if descripcion is not None:
        e["description"] = descripcion
    if lugar is not None:
        e["location"] = lugar
    if inicio:
        d_ini, f_ini = _parse(inicio)
        if d_ini:
            viejo_ini = e["start"].get("dateTime")
            viejo_fin = e["end"].get("dateTime")
            dur = (datetime.fromisoformat(viejo_fin) - datetime.fromisoformat(viejo_ini)) if viejo_ini and viejo_fin else timedelta(hours=1)
            d_fin = _parse(fin)[0] if fin else d_ini + dur
            e["start"] = {"dateTime": d_ini.isoformat(), "timeZone": TZ_NAME}
            e["end"] = {"dateTime": d_fin.isoformat(), "timeZone": TZ_NAME}
        else:
            e["start"] = {"date": f_ini}
            e["end"] = {"date": (datetime.fromisoformat(f_ini) + timedelta(days=1)).strftime("%Y-%m-%d")}
    e = svc.events().update(calendarId=cal, eventId=evento_id, body=e).execute()
    return _fmt_evento(e, cuenta, cal)


# El Calendario de Apple ignora el color de cada evento y usa el del calendario: por eso las tareas
# viven en dos calendarios propios de la cuenta de la escuela, rojo (pendientes) y verde (entregadas).
CAL_CLASES = ("🏫 Clases", "#9fc6e7")  # azul pastel
# Calendario escolar del CETI, separado por tipo para que en el iPhone cada uno tenga su color
CAL_CETI_SIN_CLASES = ("🌴 CETI · Sin clases", "#7ae7bf")    # verde menta
CAL_CETI_EVALUACIONES = ("📝 CETI · Evaluaciones", "#ffb878")  # durazno
CAL_CETI_FECHAS = ("🎓 CETI · Fechas escolares", "#b99aff")   # lila
# Semáforo de tareas: colores intensos para que resalten entre los calendarios pastel.
CAL_PENDIENTES = ("🟡 Tareas por entregar", "#f6bf26")
CAL_ENTREGADAS = ("🟢 Tareas entregadas", "#0b8043")
CAL_VENCIDAS = ("🔴 Tareas vencidas", "#d50000")
NOMBRES_VIEJOS = {"📚 Tareas pendientes": CAL_PENDIENTES, "✅ Tareas entregadas": CAL_ENTREGADAS}


def _calendario(cuenta: str, nombre_color: tuple[str, str]) -> str:
    nombre, color = nombre_color
    svc = _svc(cuenta, "calendar", "v3")
    for cal in svc.calendarList().list().execute().get("items", []):
        if NOMBRES_VIEJOS.get(cal.get("summary")) == nombre_color:  # renombra el calendario viejo, conserva sus eventos
            svc.calendars().patch(calendarId=cal["id"], body={"summary": nombre}).execute()
            cal["summary"] = nombre
        if cal.get("summary") == nombre:
            if cal.get("backgroundColor", "").lower() != color.lower():  # si cambió el color en el código
                svc.calendarList().patch(calendarId=cal["id"], colorRgbFormat=True,
                                         body={"backgroundColor": color, "foregroundColor": "#000000"}).execute()
            return cal["id"]
    cal = svc.calendars().insert(body={"summary": nombre, "timeZone": TZ_NAME}).execute()
    svc.calendarList().patch(calendarId=cal["id"], colorRgbFormat=True,
                             body={"backgroundColor": color, "foregroundColor": "#ffffff"}).execute()
    return cal["id"]


def calendario_entregadas(cuenta: str) -> str:
    return _calendario(cuenta, CAL_ENTREGADAS)


def calendario_pendientes(cuenta: str) -> str:
    return _calendario(cuenta, CAL_PENDIENTES)


def calendario_vencidas(cuenta: str) -> str:
    return _calendario(cuenta, CAL_VENCIDAS)


def evento_tarea_vencida(evento_id: str) -> bool:
    """Pasó la hora de entrega sin entregarla: título con ⏰ y se mueve al calendario rojo."""
    for c in conectadas():
        svc = _svc(c, "calendar", "v3")
        origen = calendario_pendientes(c)
        try:
            e = svc.events().get(calendarId=origen, eventId=evento_id).execute()
        except Exception:
            continue
        if e.get("status") == "cancelled":
            continue
        titulo = re.sub(r"^📚 Entrega:", "⏰ Vencida:", e.get("summary", ""))
        svc.events().patch(calendarId=origen, eventId=evento_id, body={"summary": titulo}).execute()
        svc.events().move(calendarId=origen, eventId=evento_id, destination=calendario_vencidas(c)).execute()
        return True
    return False


DIAS_NUM = {"lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6}


def _vaciar(svc, cal: str) -> int:
    borrados, tok = 0, None
    while True:  # singleEvents=False → borra series completas
        r = svc.events().list(calendarId=cal, pageToken=tok, maxResults=250).execute()
        for e in r.get("items", []):
            svc.events().delete(calendarId=cal, eventId=e["id"]).execute()
            borrados += 1
        tok = r.get("nextPageToken")
        if not tok:
            return borrados


def dias_sin_clases() -> list[str]:
    """Fechas (AAAA-MM-DD) de los eventos del calendario '🌴 CETI · Sin clases' (suspensiones, vacaciones, receso)."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else conectadas()[0]
    svc = _svc(cuenta, "calendar", "v3")
    fechas = []
    for e in svc.events().list(calendarId=_calendario(cuenta, CAL_CETI_SIN_CLASES), maxResults=250).execute().get("items", []):
        d = datetime.fromisoformat(e["start"]["date"]).date()
        fin = datetime.fromisoformat(e["end"]["date"]).date()
        while d < fin:
            fechas.append(d.isoformat())
            d += timedelta(days=1)
    return fechas


def ceti_cargar(eventos: list[dict]) -> dict:
    """Calendario escolar del CETI en 3 calendarios de colores (reemplaza su contenido).
    eventos: [{"tipo": "sin_clases"|"evaluacion"|"fecha", "titulo", "inicio" AAAA-MM-DD, "fin" AAAA-MM-DD (incluido)}]."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else conectadas()[0]
    svc = _svc(cuenta, "calendar", "v3")
    cals = {"sin_clases": _calendario(cuenta, CAL_CETI_SIN_CLASES),
            "evaluacion": _calendario(cuenta, CAL_CETI_EVALUACIONES),
            "fecha": _calendario(cuenta, CAL_CETI_FECHAS)}
    for cal in cals.values():
        _vaciar(svc, cal)
    cuenta_tipo = {k: 0 for k in cals}
    for e in eventos:
        fin = (datetime.fromisoformat(e.get("fin") or e["inicio"]) + timedelta(days=1)).strftime("%Y-%m-%d")
        svc.events().insert(calendarId=cals[e["tipo"]], body={
            "summary": e["titulo"], "description": "Calendario Escolar CETI 2026-2027",
            "start": {"date": e["inicio"]}, "end": {"date": fin}, "transparency": "transparent",
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 18 * 60}]}}).execute()
        cuenta_tipo[e["tipo"]] += 1
    return {"cuenta": cuenta, "eventos": cuenta_tipo}


def horario_cargar(clases: list[dict], desde: str, hasta: str) -> dict:
    """Carga el horario de clases como eventos semanales en el calendario "🏫 Clases" (cuenta de la escuela),
    REEMPLAZANDO todo lo que hubiera en ese calendario (sin duplicados).
    clases: [{"materia", "dia" (lunes…sábado), "inicio" "HH:MM", "fin" "HH:MM", "salon", "edificio", "profesor"}]."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else conectadas()[0]
    svc = _svc(cuenta, "calendar", "v3")
    cal = _calendario(cuenta, CAL_CLASES)
    borrados = _vaciar(svc, cal)
    try:
        sin_clases = dias_sin_clases()  # las clases no aparecen en suspensiones ni vacaciones
    except Exception:
        sin_clases = []
    d0 = datetime.fromisoformat(desde[:10]).date()
    fin_sem = hasta[:10].replace("-", "")
    creados = []
    for c in clases:
        dia = DIAS_NUM[c["dia"].strip().lower()]
        fecha = d0 + timedelta(days=(dia - d0.weekday()) % 7)  # primera vez ese día desde `desde`
        ini = datetime.fromisoformat(f"{fecha} {c['inicio']}").replace(tzinfo=TZ)
        fin = datetime.fromisoformat(f"{fecha} {c['fin']}").replace(tzinfo=TZ)
        lugar = ", ".join(x for x in (f"Edificio {c['edificio']}" if c.get("edificio") else "", c.get("salon", "")) if x)
        body = {"summary": c["materia"], "location": lugar,
                "description": "Clase" + (f" · Prof. {c['profesor']}" if c.get("profesor") else ""),
                "start": {"dateTime": ini.isoformat(), "timeZone": TZ_NAME},
                "end": {"dateTime": fin.isoformat(), "timeZone": TZ_NAME},
                "recurrence": [f"RRULE:FREQ=WEEKLY;UNTIL={fin_sem}T235959Z"] + (
                    [f"EXDATE;TZID={TZ_NAME}:" + ",".join(
                        f"{x.replace('-', '')}T{c['inicio'].replace(':', '')}00" for x in sin_clases
                        if datetime.fromisoformat(x).weekday() == dia and x >= str(fecha))]
                    if any(datetime.fromisoformat(x).weekday() == dia and x >= str(fecha) for x in sin_clases) else []),
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 20}]}}
        svc.events().insert(calendarId=cal, body=body).execute()
        creados.append(f"{c['dia']} {c['inicio']}-{c['fin']} {c['materia']}")
    return {"calendario": CAL_CLASES[0], "cuenta": cuenta, "series_borradas": borrados, "clases_creadas": creados,
            "hasta": hasta[:10]}


def tarea_agendar(titulo: str, inicio: str, descripcion: str, avisos_min: list[int]) -> str:
    """Evento de una tarea por entregar en el calendario amarillo (cuenta de la escuela). Devuelve su id."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else conectadas()[0]
    d_ini, _ = _parse(inicio)
    body = {"summary": titulo, "description": descripcion,
            "start": {"dateTime": d_ini.isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": (d_ini + timedelta(minutes=30)).isoformat(), "timeZone": TZ_NAME},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in avisos_min]}}
    e = _svc(cuenta, "calendar", "v3").events().insert(calendarId=calendario_pendientes(cuenta), body=body).execute()
    return e["id"]


def evento_tarea_hecha(evento_id: str) -> bool:
    """Tarea entregada: título con ✅ y se mueve al calendario verde (desde el amarillo, el rojo o el principal).
    Solo en la cuenta donde tarea_agendar crea los eventos: _calendario() crea el calendario si
    no existe, y recorrer todas las cuentas los creaba también en la personal."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else (conectadas() or [None])[0]
    for c in [cuenta] if cuenta else []:
        svc = _svc(c, "calendar", "v3")
        destino = calendario_entregadas(c)
        for origen in (calendario_pendientes(c), calendario_vencidas(c), "primary"):
            try:
                e = svc.events().get(calendarId=origen, eventId=evento_id).execute()
            except Exception:
                continue
            if e.get("status") == "cancelled":  # copia que deja Google en el calendario de donde se movió
                continue
            titulo = re.sub(r"^📚 Entrega:", "✅ Entregada:", e.get("summary", ""))
            titulo = re.sub(r"^⏰ Vencida:", "✅ Entregada (tarde):", titulo)
            svc.events().patch(calendarId=origen, eventId=evento_id, body={"summary": titulo}).execute()
            svc.events().move(calendarId=origen, eventId=evento_id, destination=destino).execute()
            return True
    return False


def cal_obtener(cuenta: str, evento_id: str, calendario_id: str = "primary") -> dict:
    e = _svc(cuenta, "calendar", "v3").events().get(calendarId=calendario_id or "primary", eventId=evento_id).execute()
    return _fmt_evento(e, cuenta, calendario_id or "primary")


def cal_borrar(cuenta: str, evento_id: str, calendario_id: str = "primary"):
    """Con el serie_id borra la serie completa de un evento que se repite."""
    _svc(cuenta, "calendar", "v3").events().delete(calendarId=calendario_id or "primary", eventId=evento_id).execute()


# ---------- Gmail ----------

def _header(msg: dict, nombre: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == nombre.lower():
            return h["value"]
    return ""


def gmail_buscar(consulta: str = "is:unread newer_than:2d category:primary", cuenta: str = "", maximo: int = 10) -> list[dict]:
    """Sintaxis de Gmail: from:, to:, subject:, has:attachment, filename:pdf, is:unread, is:starred,
    label:, newer_than:7d, older_than:, after:2026/09/01, before:, category:primary|promotions|social."""
    cuentas = [cuenta] if cuenta else conectadas()
    res = []
    for c in cuentas:
        svc = _svc(c, "gmail", "v1")
        ids = svc.users().messages().list(userId="me", q=consulta, maxResults=min(maximo, 25)).execute().get("messages", [])
        for m in ids:
            msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Subject", "Date"]).execute()
            res.append({"id": m["id"], "hilo": msg.get("threadId"), "cuenta": c, "de": _header(msg, "From"),
                        "asunto": _header(msg, "Subject"), "fecha": _header(msg, "Date"),
                        "no_leido": "UNREAD" in msg.get("labelIds", []), "resumen": msg.get("snippet", "")[:200]})
    return res


def _texto_de(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for parte in payload.get("parts", []) or []:
        t = _texto_de(parte)
        if t:
            return t
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def gmail_leer(cuenta: str, mensaje_id: str) -> dict:
    msg = _svc(cuenta, "gmail", "v1").users().messages().get(userId="me", id=mensaje_id, format="full").execute()
    return {"id": mensaje_id, "cuenta": cuenta, "hilo": msg.get("threadId"),
            "de": _header(msg, "From"), "para": _header(msg, "To"), "cc": _header(msg, "Cc"),
            "asunto": _header(msg, "Subject"), "fecha": _header(msg, "Date"), "message_id": _header(msg, "Message-ID"),
            "adjuntos": [p["filename"] for p in _partes(msg.get("payload", {})) if p.get("filename")],
            "cuerpo": re.sub(r"\n{3,}", "\n\n", _texto_de(msg.get("payload", {})))[:6000]}


def gmail_hilo(cuenta: str, hilo_id: str) -> list[dict]:
    """Toda la conversación, en orden."""
    t = _svc(cuenta, "gmail", "v1").users().threads().get(userId="me", id=hilo_id, format="full").execute()
    return [{"id": m["id"], "de": _header(m, "From"), "fecha": _header(m, "Date"),
             "cuerpo": re.sub(r"\n{3,}", "\n\n", _texto_de(m.get("payload", {})))[:2500]} for m in t.get("messages", [])]


def gmail_etiquetas(cuenta: str) -> list[dict]:
    r = _svc(cuenta, "gmail", "v1").users().labels().list(userId="me").execute()
    return [{"id": l["id"], "nombre": l["name"], "tipo": l.get("type")} for l in r.get("labels", [])]


def _etiqueta_id(cuenta: str, nombre: str) -> str:
    for l in gmail_etiquetas(cuenta):
        if l["nombre"].lower() == nombre.lower() or l["id"] == nombre:
            return l["id"]
    return _svc(cuenta, "gmail", "v1").users().labels().create(userId="me", body={"name": nombre}).execute()["id"]


def gmail_marcar(cuenta: str, ids: list[str], accion: str) -> str:
    """accion: leido | no_leido | archivar | desarchivar | destacar | quitar_destacado | importante |
    papelera | etiqueta:<nombre> | quitar_etiqueta:<nombre>."""
    svc = _svc(cuenta, "gmail", "v1").users().messages()
    a, _, arg = accion.partition(":")
    if a == "papelera":
        for i in ids:
            svc.trash(userId="me", id=i).execute()
        return f"{len(ids)} a la papelera"
    add, rem = {
        "leido": ([], ["UNREAD"]), "no_leido": (["UNREAD"], []), "archivar": ([], ["INBOX"]),
        "desarchivar": (["INBOX"], []), "destacar": (["STARRED"], []), "quitar_destacado": ([], ["STARRED"]),
        "importante": (["IMPORTANT"], []),
        "etiqueta": ([_etiqueta_id(cuenta, arg)] if arg else [], []),
        "quitar_etiqueta": ([], [_etiqueta_id(cuenta, arg)] if arg else []),
    }.get(a, (None, None))
    if add is None:
        raise ValueError(f"acción no válida: {accion}")
    svc.batchModify(userId="me", body={"ids": ids, "addLabelIds": add, "removeLabelIds": rem}).execute()
    return f"{len(ids)} correo(s): {accion}"


def _ids(svc, consulta: str, maximo: int) -> list[str]:
    ids, tok = [], None
    while len(ids) < maximo:
        r = svc.users().messages().list(userId="me", q=consulta, maxResults=min(500, maximo - len(ids)),
                                        pageToken=tok).execute()
        ids += [m["id"] for m in r.get("messages", [])]
        tok = r.get("nextPageToken")
        if not tok:
            break
    return ids


def _partes(payload: dict):
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _partes(p)


def _nombre_seguro(n: str) -> str:
    n = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\s+(\.\w+)$", r"\1", n)  # "IGS01 2026 .pdf" → "IGS01 2026.pdf"
    return n[:120] or "archivo"


def gmail_guardar_adjuntos(consulta: str, destino: Path, cuenta: str = "", maximo: int = 400) -> dict:
    """Descarga los adjuntos de TODOS los correos que coinciden (de una cuenta o de todas).
    Si un correo no trae adjuntos, guarda su contenido como .html/.txt (hay recibos que vienen en el cuerpo).
    Nombres: AAAA-MM-DD_<nombre>. No repite archivos ya descargados."""
    destino.mkdir(parents=True, exist_ok=True)
    res = {"correos": 0, "archivos": 0, "cuerpos": 0, "ya_existian": 0, "errores": [], "desde": None, "hasta": None}
    for c in ([cuenta] if cuenta else conectadas()):
        svc = _svc(c, "gmail", "v1")
        for mid in _ids(svc, consulta, maximo):
            try:
                msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            except Exception as e:
                res["errores"].append(f"{mid}: {e}")
                continue
            res["correos"] += 1
            fecha = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, TZ).strftime("%Y-%m-%d")
            res["desde"] = min(res["desde"] or fecha, fecha)
            res["hasta"] = max(res["hasta"] or fecha, fecha)
            adjuntos = [p for p in _partes(msg["payload"]) if p.get("filename")]
            for i, p in enumerate(adjuntos):
                nombre = _nombre_seguro(p["filename"])
                ruta = destino / f"{fecha}_{nombre}"
                if ruta.exists() and ruta.stat().st_size != int(p.get("body", {}).get("size") or -1):
                    # mismo nombre y día pero otro archivo (p. ej. dos "recibo.pdf"): se distingue por correo
                    base, punto, ext = nombre.rpartition(".")
                    unico = f"{base}_{mid[-6:]}.{ext}" if punto else f"{nombre}_{mid[-6:]}"
                    ruta = destino / f"{fecha}_{unico}"
                if ruta.exists():
                    res["ya_existian"] += 1
                    continue
                body = p.get("body", {})
                try:
                    data = body.get("data") or svc.users().messages().attachments().get(
                        userId="me", messageId=mid, id=body["attachmentId"]).execute()["data"]
                    ruta.write_bytes(base64.urlsafe_b64decode(data))
                    res["archivos"] += 1
                except Exception as e:
                    res["errores"].append(f"{p['filename']}: {e}")
            if not adjuntos:
                asunto = _nombre_seguro(_header(msg, "Subject") or "correo")[:60]
                html_part = next((p for p in _partes(msg["payload"]) if p.get("mimeType") == "text/html" and p.get("body", {}).get("data")), None)
                if html_part:
                    ruta = destino / f"{fecha}_{asunto}_{mid[-6:]}.html"
                    contenido = base64.urlsafe_b64decode(html_part["body"]["data"])
                else:
                    ruta = destino / f"{fecha}_{asunto}_{mid[-6:]}.txt"
                    contenido = _texto_de(msg["payload"]).encode()
                if ruta.exists():
                    res["ya_existian"] += 1
                else:
                    ruta.write_bytes(contenido)
                    res["cuerpos"] += 1
    res["errores"] = res["errores"][:5]
    return res


def _mime(cuenta: str, para: str, asunto: str, cuerpo: str, responder_a: str = "",
          adjuntos: list[Path] | None = None, cc: str = "") -> dict:
    if adjuntos:
        mime = MIMEMultipart()
        mime.attach(MIMEText(cuerpo, "plain", "utf-8"))
        for ruta in adjuntos:
            tipo = (mimetypes.guess_type(ruta.name)[0] or "application/octet-stream").split("/", 1)
            parte = MIMEBase(*tipo)
            parte.set_payload(ruta.read_bytes())
            encoders.encode_base64(parte)
            parte.add_header("Content-Disposition", "attachment", filename=ruta.name)
            mime.attach(parte)
    else:
        mime = MIMEText(cuerpo, "plain", "utf-8")
    mime["To"] = para
    if cc:
        mime["Cc"] = cc
    mime["Subject"] = asunto
    body = {}
    if responder_a:
        orig = gmail_leer(cuenta, responder_a)
        mime["In-Reply-To"] = orig["message_id"]
        mime["References"] = orig["message_id"]
        body["threadId"] = orig["hilo"]
    body["raw"] = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    return body


def gmail_enviar(cuenta: str, para: str, asunto: str, cuerpo: str, responder_a: str = "",
                 adjuntos: list[Path] | None = None, cc: str = "") -> str:
    body = _mime(cuenta, para, asunto, cuerpo, responder_a, adjuntos, cc)
    return _svc(cuenta, "gmail", "v1").users().messages().send(userId="me", body=body).execute().get("id", "")


def gmail_borrador(cuenta: str, para: str, asunto: str, cuerpo: str, responder_a: str = "",
                   adjuntos: list[Path] | None = None, cc: str = "") -> str:
    body = _mime(cuenta, para, asunto, cuerpo, responder_a, adjuntos, cc)
    return _svc(cuenta, "gmail", "v1").users().drafts().create(userId="me", body={"message": body}).execute()["id"]


# ---------- Classroom ----------

def _due(cw: dict) -> datetime | None:
    d = cw.get("dueDate")
    if not d:
        return None
    t = cw.get("dueTime")
    if t:  # Classroom da la hora en UTC
        dt = datetime(d["year"], d["month"], d["day"], t.get("hours", 0), t.get("minutes", 0), tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(TZ)
    return datetime(d["year"], d["month"], d["day"], 23, 59, tzinfo=TZ)


def classroom_cursos() -> list[dict]:
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().list(studentId="me", courseStates=["ACTIVE"], pageSize=50).execute(num_retries=3)
    return [{"id": c["id"], "nombre": c["name"], "seccion": c.get("section", ""), "link": c.get("alternateLink", "")}
            for c in r.get("courses", [])]


_temas_cache: dict[str, tuple[float, dict]] = {}


def classroom_temas(curso_id: str) -> dict[str, str]:
    """{topicId: nombre} de un curso (cacheado 1 h). Sin permiso de temas devuelve {}."""
    ts, val = _temas_cache.get(curso_id, (0, {}))
    if time.time() - ts > 3600:
        try:
            r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().topics().list(courseId=curso_id).execute(num_retries=2)
            val = {t["topicId"]: t["name"] for t in r.get("topic", [])}
        except Exception:
            val = {}
        _temas_cache[curso_id] = (time.time(), val)
    return val


def parcial_de_tema(tema: str) -> str | None:
    """'Parcial 2', 'P2', 'Segundo parcial', '2do parcial' → 'P2'."""
    t = (tema or "").lower()
    if m := re.search(r"\bp\s*([1-3])\b|parcial\s*([1-3])|([1-3])\s*(?:er|do|ro|°|º)?\s*parcial", t):
        return "P" + next(x for x in m.groups() if x)
    for palabra, n in (("primer", "1"), ("segund", "2"), ("tercer", "3")):
        if re.search(palabra + r"\w*\s+parcial", t):
            return "P" + n
    return None


def _utc_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]


def agregar_fecha_escolar(tipo: str, titulo: str, inicio: str, fin: str = "", descripcion: str = "") -> str:
    """Fecha sacada de un anuncio → calendario de color según su tipo (cuenta escuela)."""
    cuenta = CUENTA_CLASSROOM if CUENTA_CLASSROOM in conectadas() else conectadas()[0]
    cal = _calendario(cuenta, {"examen": CAL_CETI_EVALUACIONES, "entrega": CAL_PENDIENTES}.get(tipo, CAL_CETI_FECHAS))
    svc = _svc(cuenta, "calendar", "v3")
    # ¿ya está? (mismo día y título parecido) → no duplicar
    dia = inicio[:10]
    fin_dia = (datetime.fromisoformat(dia) + timedelta(days=1)).strftime("%Y-%m-%d")
    palabras = lambda t: {w for w in re.findall(r"\w+", t.lower()) if len(w) > 3}
    for e in svc.events().list(calendarId=cal, timeMin=f"{dia}T00:00:00-06:00", timeMax=f"{fin_dia}T00:00:00-06:00",
                               singleEvents=True).execute().get("items", []):
        if len(palabras(e.get("summary", "")) & palabras(titulo)) >= 2:
            return {"examen": "📝 Evaluaciones", "entrega": "🟡 Tareas por entregar"}.get(tipo, "🎓 Fechas escolares") + " (ya estaba)"
    d_ini, f_ini = _parse(inicio)
    body = {"summary": titulo, "description": descripcion,
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 18 * 60}, {"method": "popup", "minutes": 120}]}}
    if d_ini:
        d_fin = _parse(fin)[0] if fin and len(fin) > 10 else d_ini + timedelta(hours=1)
        body["start"] = {"dateTime": d_ini.isoformat(), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": d_fin.isoformat(), "timeZone": TZ_NAME}
    else:
        body["start"] = {"date": f_ini}
        body["end"] = {"date": (datetime.fromisoformat((fin or f_ini)[:10]) + timedelta(days=1)).strftime("%Y-%m-%d")}
    svc.events().insert(calendarId=cal, body=body).execute()
    return {"examen": "📝 Evaluaciones", "entrega": "🟡 Tareas por entregar"}.get(tipo, "🎓 Fechas escolares")


def classroom_profesores(curso_id: str) -> list[dict]:
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().teachers().list(courseId=curso_id).execute(num_retries=2)
    return [{"nombre": t.get("profile", {}).get("name", {}).get("fullName", ""),
             "correo": t.get("profile", {}).get("emailAddress", "")} for t in r.get("teachers", [])]


def classroom_anuncios(curso_id: str, n: int = 5) -> list[dict]:
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().announcements().list(
        courseId=curso_id, pageSize=n, orderBy="updateTime desc").execute(num_retries=2)
    return [{"id": a["id"], "creado": a.get("creationTime", ""),
             "fecha": _utc_local(a.get("creationTime", "")), "texto": a.get("text", "")[:3000],
             "link": a.get("alternateLink", ""),
             "adjuntos": [next(iter(m.values())).get("title", "") if isinstance(next(iter(m.values())), dict) else ""
                          for m in a.get("materials", [])]} for a in r.get("announcements", [])]


def classroom_materiales(curso_id: str, n: int = 10) -> list[dict]:
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().courseWorkMaterials().list(
        courseId=curso_id, pageSize=n, orderBy="updateTime desc").execute(num_retries=2)
    temas = classroom_temas(curso_id)
    return [{"titulo": m.get("title", ""), "descripcion": m.get("description", "")[:800],
             "tema": temas.get(m.get("topicId", ""), ""), "link": m.get("alternateLink", ""),
             "fecha": m.get("creationTime", "")[:10]} for m in r.get("courseWorkMaterial", [])]


def classroom_mis_tareas(curso_id: str, curso_nombre: str, n: int = 30) -> list[dict]:
    """Tareas de un curso con MI estado de entrega y calificación."""
    svc = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses()
    temas = classroom_temas(curso_id)
    subs = {s["courseWorkId"]: s for s in svc.courseWork().studentSubmissions().list(
        courseId=curso_id, courseWorkId="-", userId="me", pageSize=100).execute(num_retries=2).get("studentSubmissions", [])}
    res = []
    for t in classroom_tareas(curso_id, curso_nombre, n):
        s = subs.get(t["id"], {})
        res.append({"id": t["id"], "curso": curso_nombre, "titulo": t["titulo"], "entrega": (t["entrega"] or "")[:16],
                    "tema": temas.get(t.get("tema_id", ""), ""), "estado": s.get("state", ""),
                    "tarde": s.get("late", False), "calificacion": s.get("assignedGrade"),
                    "borrador_calificacion": s.get("draftGrade"), "puntos_max": t.get("puntos_max"),
                    "link": t["link"]})
    return res


def classroom_tareas(curso_id: str, curso_nombre: str, n: int = 20) -> list[dict]:
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().courseWork().list(
        courseId=curso_id, orderBy="updateTime desc", pageSize=n).execute(num_retries=3)
    tareas = []
    for cw in r.get("courseWork", []):
        due = _due(cw)
        tareas.append({
            "id": cw["id"], "curso_id": curso_id, "curso": curso_nombre, "titulo": cw.get("title", ""),
            "descripcion": cw.get("description", ""), "link": cw.get("alternateLink", ""),
            "entrega": due.isoformat() if due else None, "materiales": cw.get("materials", []),
            "creada": cw.get("creationTime", ""), "tema_id": cw.get("topicId", ""),
            "puntos_max": cw.get("maxPoints"),
        })
    return tareas


def classroom_estado(curso_id: str, tarea_id: str) -> str:
    """NEW / CREATED / TURNED_IN / RETURNED / RECLAIMED_BY_STUDENT."""
    r = _svc(CUENTA_CLASSROOM, "classroom", "v1").courses().courseWork().studentSubmissions().list(
        courseId=curso_id, courseWorkId=tarea_id, userId="me").execute(num_retries=3)
    subs = r.get("studentSubmissions", [])
    return subs[0].get("state", "") if subs else ""


EXPORTS = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


def descargar_materiales(materiales: list[dict], destino: Path) -> tuple[list[str], list[str]]:
    """Descarga los archivos de Drive a `destino`. Devuelve (archivos, links_no_descargables)."""
    destino.mkdir(parents=True, exist_ok=True)
    drive = _svc(CUENTA_CLASSROOM, "drive", "v3")
    archivos, links = [], []
    for m in materiales:
        if "driveFile" in m:
            f = m["driveFile"]["driveFile"]
            try:
                meta = drive.files().get(fileId=f["id"], fields="name,mimeType").execute()
                nombre = re.sub(r'[\\/:*?"<>|]', "_", meta["name"])
                if meta["mimeType"] in EXPORTS:
                    mime, ext = EXPORTS[meta["mimeType"]]
                    req = drive.files().export_media(fileId=f["id"], mimeType=mime)
                    nombre += ext
                elif meta["mimeType"].startswith("application/vnd.google-apps"):
                    req = drive.files().export_media(fileId=f["id"], mimeType="application/pdf")
                    nombre += ".pdf"
                else:
                    req = drive.files().get_media(fileId=f["id"])
                buf = io.BytesIO()
                dl = MediaIoBaseDownload(buf, req)
                done = False
                while not done:
                    _, done = dl.next_chunk()
                (destino / nombre).write_bytes(buf.getvalue())
                archivos.append(nombre)
            except Exception as e:
                links.append(f"{f.get('title', 'archivo')}: {f.get('alternateLink', '')} (no se pudo bajar: {e})")
        elif "link" in m:
            links.append(f"{m['link'].get('title', 'link')}: {m['link']['url']}")
        elif "youtubeVideo" in m:
            links.append(f"Video: {m['youtubeVideo'].get('title', '')} {m['youtubeVideo'].get('alternateLink', '')}")
        elif "form" in m:
            links.append(f"Formulario: {m['form'].get('title', '')} {m['form'].get('formUrl', '')}")
    return archivos, links
