"""
Lote diario de correos de empleo (Gmail "empleo"): a contactos PUBLICADOS para recibir CVs (los junta la busqueda
de empleos.py), un correo personalizado por empresa con su CV adjunto. Chava aprueba el lote con un boton.

Archivos (Empleo/Correos):
  contactos.json   {correo, empresa, perfil, fuente, nota, encontrado, estado, enviado, seguimientos, respondio}
                   estado: pendiente | enviado | respondio | no_contactar | rebote
  envios.json      historial de lo mandado {correo, empresa, fecha, tipo (primero|seguimiento), asunto, gmail_id}
  lote.json        el lote del dia esperando su boton {fecha, items: [{correo, empresa, perfil, tipo, asunto, cuerpo}]}
  borradores.json  lo que deja Claude al redactar (se integra a lote.json)

Para que Gmail no bloquee una cuenta nueva: calentamiento (5/dia la 1a semana, 10 la 2a, luego MAX_DIA), nunca
la misma empresa dos veces en 60 dias, un solo seguimiento a los 7 dias, y pausa entre correos.
"""
import json
import re
from email.utils import parsedate_to_datetime
import unicodedata
from datetime import date, timedelta

import empleos

DIR = empleos.DIR / "Correos"
CONTACTOS = DIR / "contactos.json"
ENVIOS = DIR / "envios.json"
LOTE = DIR / "lote.json"
BORRADORES = DIR / "borradores.json"
BANDEJA = DIR / "bandeja.json"      # lo ultimo que llego a su Gmail de empleo (para la app: Jobs → Correos)
PEDIDOS = DIR / "pedidos"           # la app deja aqui {"id": <mensaje>} y KORRA le manda ese correo por Telegram
CUENTA_JSON = DIR / "cuenta.json"   # {"correo": "<su gmail de empleo>"} para los links a Gmail
MAX_BANDEJA = 40
# Lo que importa de su bandeja: de una empresa a la que escribio, o que hable de trabajo (lo demas, p. ej. Meetup o
# avisos del banco, la app lo esconde en "ver los otros").
IMPORTANTE = re.compile(r"emple|vacante|entrevista|postulaci|candidat|\bcv\b|curr[ií]cul|pr[aá]ctica|becari|reclut|"
                        r"talento|recursos humanos|\brh\b|oferta|contrataci|proceso de selecci|interview|job|career", re.I)
CWD = empleos.CWD
DIAS_SIN_REPETIR = 60
DIAS_SEGUIMIENTO = 7
NEGATIVAS = re.compile(r"no (me )?(vuelvas a )?(escrib|contact|mand)|no (nos )?interesa|unsubscribe|remove me|"
                       r"dar(me)? de baja|no estamos contratando|not interested", re.I)

PROMPT = """Redacta correos para que Salvador Pantoja (Chava) pida informes de vacantes, prácticas o becas y deje su CV.
Primero lee Perfil/perfil.md, Perfil/perfil-software.md, Perfil/perfil-electronica.md y Perfil/cv_actual.md.
Si sirve, abre (WebFetch) la "fuente" de cada contacto para saber qué hace la empresa o qué piden.

Contactos de hoy (JSON):
{contactos}

Reglas para CADA correo:
- Español, cálido y profesional, corto (90-150 palabras), sin plantillas genéricas: menciona algo concreto de esa
  empresa o de lo que piden, y 1-2 cosas de Chava que de verdad le sirvan a ELLOS.
- SEPARA LOS PERFILES (muy importante): si el contacto es de **software/IT**, preséntalo como estudiante de
  Ingeniería en Desarrollo de Software con proyectos reales (su servidor Linux con Docker, sus apps en Python/Go/
  Flutter) que busca su primer puesto en tech. NO lo presentes como técnico de diagnóstico ni hables de pruebas,
  tarjetas o producción: si un reclutador de IT ve eso lo manda a producción, y él no quiere eso (sobre todo en
  empresas de manufactura como Jabil, Flex o Sanmina). Si acaso: "trabajo actualmente en la industria, pero busco
  dar el salto a tecnología". Si el contacto es de **electrónica**: ahí sí, su experiencia en Foxconn (servidores
  NVIDIA GB200/GB300) y Plexus (ICT, flying probe), buscando un puesto mejor (ingeniería de pruebas, técnico senior).
- Adjuntas el CV de SU perfil (software o electrónica), nunca el otro.
- Pide información de vacantes/prácticas/becas y menciona que adjunta su CV. Cierra con su nombre completo,
  teléfono +52 33 0000 0000 y que está en Guadalajara.
- 100 % honesto: nada que no esté en sus archivos. Sin mayúsculas gritonas, sin emojis, sin promesas exageradas.
- "tipo": "seguimiento" = segundo y último correo (60-90 palabras): amable, recuerda el anterior, sin presionar.
- Asunto claro y específico (ej. "Prácticas en soporte TI — Salvador Pantoja, CV adjunto").

Escribe Correos/borradores.json (sobrescríbelo) con este formato exacto:
{{"correos": [{{"correo": "...", "asunto": "...", "cuerpo": "texto plano con saltos de línea"}}]}}
No crees ni modifiques ningún otro archivo. Tu respuesta final: cuántos redactaste, en una línea."""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def contactos() -> list[dict]:
    return empleos._leer(CONTACTOS)


def envios() -> list[dict]:
    return empleos._leer(ENVIOS)


def limite_hoy(hoy: date, maximo: int, historial: list[dict] | None = None) -> int:
    """Cuantos correos se pueden mandar hoy: calentamiento de la cuenta nueva menos lo ya mandado hoy."""
    historial = envios() if historial is None else historial
    fechas = sorted(e["fecha"][:10] for e in historial)
    if not fechas:
        tope = min(5, maximo)
    else:
        dias = (hoy - date.fromisoformat(fechas[0])).days
        tope = min(5 if dias < 7 else 10 if dias < 14 else maximo, maximo)
    return max(0, tope - sum(1 for f in fechas if f == hoy.isoformat()))


def seleccionar(hoy: date, cupo: int) -> list[dict]:
    """El lote: primero los seguimientos que tocan, luego contactos nuevos (los mas viejos primero).
    Nunca la misma empresa dos veces en el lote ni en los ultimos DIAS_SIN_REPETIR (salvo su seguimiento)."""
    if cupo <= 0:
        return []
    cs = contactos()
    recientes = {_norm(e.get("empresa", "")) for e in envios()
                 if date.fromisoformat(e["fecha"][:10]) > hoy - timedelta(days=DIAS_SIN_REPETIR)}
    lote, empresas = [], set()
    for c in cs:  # seguimientos
        if (c.get("estado") == "enviado" and not c.get("seguimientos") and c.get("enviado")
                and date.fromisoformat(c["enviado"][:10]) <= hoy - timedelta(days=DIAS_SEGUIMIENTO)
                and _norm(c["empresa"]) not in empresas):
            lote.append(c | {"tipo": "seguimiento"})
            empresas.add(_norm(c["empresa"]))
    for c in sorted(cs, key=lambda c: c.get("encontrado", "")):
        e = _norm(c.get("empresa", "")) or c["correo"]
        if c.get("estado") == "pendiente" and e not in recientes and e not in empresas:
            lote.append(c | {"tipo": "primero"})
            empresas.add(e)
    return [{k: c.get(k) for k in ("correo", "empresa", "perfil", "fuente", "nota", "tipo")} for c in lote[:cupo]]


def prompt_borradores(lote: list[dict]) -> str:
    return PROMPT.format(contactos=json.dumps(lote, ensure_ascii=False, indent=1))


def armar_lote(lote: list[dict], hoy: date) -> dict | None:
    """Junta lo que redacto Claude con el lote y lo guarda esperando el boton. None si no redacto nada."""
    try:
        redactados = {c["correo"].lower(): c for c in json.loads(BORRADORES.read_text(encoding="utf-8"))["correos"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    items = []
    for c in lote:
        r = redactados.get(c["correo"].lower())
        if r and str(r.get("asunto", "")).strip() and str(r.get("cuerpo", "")).strip():
            items.append(c | {"asunto": r["asunto"].strip()[:200], "cuerpo": r["cuerpo"].strip()[:4000]})
    if not items:
        return None
    datos = {"fecha": hoy.isoformat(), "items": items}
    empleos._escribir(LOTE, datos)
    return datos


def lote_actual() -> dict | None:
    try:
        d = json.loads(LOTE.read_text(encoding="utf-8"))
        return d if d.get("items") else None
    except (OSError, ValueError):
        return None


def quitar(i: int) -> dict | None:
    d = lote_actual()
    if d and 0 <= i < len(d["items"]):
        d["items"].pop(i)
        empleos._escribir(LOTE, d)
    return lote_actual()


def descartar_lote():
    LOTE.unlink(missing_ok=True)


def adjunto(perfil: str):
    """El CV que va: el general del perfil si ya existe (CVs/00_Maestros/Salvador_Pantoja_CV_<Perfil>.pdf),
    si no el Master."""
    base = empleos.DIR / "CVs" / "00_Maestros"
    propio = base / f"Salvador_Pantoja_CV_{'Software' if perfil == 'software' else 'Electronica'}.pdf"
    return propio if propio.exists() else base / "Salvador_Pantoja_CV_Master.pdf"


def registrar_envio(item: dict, gmail_id: str, ahora: str):
    cs = contactos()
    for c in cs:
        if c["correo"].lower() == item["correo"].lower():
            if item["tipo"] == "seguimiento":
                c["seguimientos"] = c.get("seguimientos", 0) + 1
            else:
                c["estado"], c["enviado"] = "enviado", ahora
    empleos._escribir(CONTACTOS, cs)
    empleos._escribir(ENVIOS, envios() + [{"correo": item["correo"], "empresa": item["empresa"], "fecha": ahora,
                                           "tipo": item["tipo"], "asunto": item["asunto"], "gmail_id": gmail_id}])


def procesar_respuestas(mensajes: list[dict], ahora: str) -> list[dict]:
    """Correos que llegaron a la cuenta de empleo ({de, asunto, cuerpo}): marca quien contesto, quien dijo que no y los
    rebotes. Devuelve los que importan para avisarle a Chava."""
    cs = contactos()
    por_correo = {c["correo"].lower(): c for c in cs}
    por_dominio = {c["correo"].lower().split("@")[1]: c for c in cs if c.get("estado") in ("enviado", "respondio")}
    avisar = []
    for m in mensajes:
        remitente = (re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", m.get("de", "")) or [""])[0].lower()
        texto = f"{m.get('asunto', '')}\n{m.get('cuerpo', '')}"
        if "mailer-daemon" in remitente or "postmaster" in remitente:
            fallido = next((c for k, c in por_correo.items() if k in texto.lower()), None)
            if fallido:
                fallido["estado"] = "rebote"
                avisar.append({"tipo": "rebote", "empresa": fallido["empresa"], "correo": fallido["correo"]})
            continue
        c = por_correo.get(remitente) or por_dominio.get(remitente.split("@")[-1])
        if not c:
            continue
        if NEGATIVAS.search(texto):
            c["estado"] = "no_contactar"
        else:
            c["estado"] = "respondio"
        c["respondio"] = ahora
        avisar.append({"tipo": c["estado"], "empresa": c["empresa"], "correo": remitente, "asunto": m.get("asunto", "")})
    if avisar:
        empleos._escribir(CONTACTOS, cs)
    return avisar


def resumen() -> dict:
    cs = contactos()
    cuenta = {e: sum(1 for c in cs if c.get("estado") == e) for e in ("pendiente", "enviado", "respondio", "no_contactar", "rebote")}
    return cuenta | {"total": len(cs), "enviados_total": len(envios())}



# ---------------------------------------------------------------- bandeja y pedidos de la app

def _remitente(de: str) -> str:
    return ((re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", de or "") or [""])[0]).lower()


def empresa_de(correo: str) -> str:
    """La empresa del contacto que escribio (por correo exacto o por dominio), o ''."""
    cs = contactos()
    exacto = next((c for c in cs if c["correo"].lower() == correo), None)
    if exacto:
        return exacto.get("empresa", "")
    dominio = correo.split("@")[-1]
    return next((c.get("empresa", "") for c in cs if c["correo"].lower().split("@")[-1] == dominio), "")


def _fecha_iso(texto: str) -> str:
    try:
        return parsedate_to_datetime(texto).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def guardar_bandeja(mensajes: list[dict]):
    """mensajes = lo que da gmail_buscar ({id, hilo, de, asunto, fecha, resumen}). Se guardan los MAX_BANDEJA mas nuevos."""
    actual = {m["id"]: m for m in empleos._leer(BANDEJA)}
    for m in mensajes:
        correo = _remitente(m.get("de", ""))
        actual[m["id"]] = {"id": m["id"], "hilo": m.get("hilo") or "", "de": m.get("de", ""), "correo": correo,
                           "empresa": empresa_de(correo), "asunto": m.get("asunto", ""),
                           "fecha": _fecha_iso(m.get("fecha", "")) or actual.get(m["id"], {}).get("fecha", ""),
                           "extracto": (m.get("resumen") or "")[:300],
                           "rebote": "mailer-daemon" in correo or "postmaster" in correo}
        x = actual[m["id"]]
        x["importante"] = bool(x["empresa"]) or x["rebote"] or bool(IMPORTANTE.search(f"{x['asunto']} {x['de']} {x['extracto']}"))
    lista = sorted(actual.values(), key=lambda x: x.get("fecha", ""), reverse=True)[:MAX_BANDEJA]
    empleos._escribir(BANDEJA, lista)


def bandeja() -> list[dict]:
    return empleos._leer(BANDEJA)


def guardar_cuenta(correo: str):
    empleos._escribir(CUENTA_JSON, {"correo": correo})  # type: ignore[arg-type]


def url_gmail(hilo: str) -> str:
    try:
        correo = json.loads(CUENTA_JSON.read_text(encoding="utf-8")).get("correo", "")
    except (OSError, ValueError):
        correo = ""
    return f"https://mail.google.com/mail/u/{correo or 0}/#all/{hilo}" if hilo else ""


def pedidos_pendientes() -> list[tuple]:
    """(ruta, {"id": ...}) que dejo la app, los mas viejos primero."""
    out = []
    for ruta in sorted(PEDIDOS.glob("*.json")) if PEDIDOS.is_dir() else []:
        try:
            out.append((ruta, json.loads(ruta.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            ruta.unlink(missing_ok=True)
    return out
