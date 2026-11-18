"""
Bot de Telegram: asistente con DeepSeek que delega el trabajo pesado a Claude Code.

- Solo responde a ALLOWED_CHAT_ID.
- DeepSeek platica y decide; cuando hay que crear/editar documentos, investigar o
  programar, llama la herramienta `delegar_a_claude` con un prompt detallado.
- El worker ejecuta Claude Code sobre CETI COLOMOS; al terminar, el bot manda el resumen
  y los archivos nuevos/modificados.
"""
import asyncio
import ctypes
import gc
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import html

import basedatos
import estadisticas
import estado_servidor
import google_svc as g
import habilidades
import correos_empleo as ce
import empleos
import impresora
import jarvis
import memoria
import remoto
import voz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
WORKER_URL = os.environ.get("WORKER_URL", "http://worker:8000")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
CREACIONES = Path(os.environ.get("CREACIONES_DIR", "/creaciones"))  # videos, diseños y presentaciones (no escolares)
SEMESTRE_FORZADO = os.environ.get("SEMESTRE_ACTUAL", "").strip()
if SEMESTRE_FORZADO.isdigit():  # permite SEMESTRE_ACTUAL=1 además de SEMESTRE_1
    SEMESTRE_FORZADO = f"SEMESTRE_{SEMESTRE_FORZADO}"
ENTRADA_DIR = "_TELEGRAM/entrada"  # relativo a WORKSPACE; fuera de los semestres
NOMBRE = os.environ.get("ASISTENTE_NOMBRE", "Jarvis")
HORA_MANANA = os.environ.get("HORA_MANANA", "08:00")   # "" desactiva el mensaje
HORA_NOCHE = os.environ.get("HORA_NOCHE", "22:00")
HORA_REFLEXION = os.environ.get("HORA_REFLEXION", "03:30")
HORA_REFLEXION_SEMANAL = os.environ.get("HORA_REFLEXION_SEMANAL", "04:00")  # solo los domingos, antes del resumen de la mañana
CLASSROOM_CADA_MIN = int(os.environ.get("CLASSROOM_CADA_MIN", "15"))  # cada cuánto busca tareas nuevas
HISTORY_LEN = 40
MAX_FILE_MB = 49  # límite de Telegram para bots: 50 MB
MAX_FILES = 10

deepseek = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
worker = httpx.AsyncClient(base_url=WORKER_URL, timeout=30)


async def deepseek_chat(origen: str, **kw):
    """Toda llamada a DeepSeek pasa por aquí para que quede medida (tokens y $) en las estadísticas.
    Si DeepSeek está saturado a veces contesta vacío (el SDK devuelve None): se reintenta una vez."""
    for intento in range(2):
        resp = await deepseek.chat.completions.create(**kw)
        if resp is not None and resp.choices:
            estadisticas.registrar(db, origen, resp)
            return resp
        log.warning("DeepSeek contestó vacío (%s, intento %d)", origen, intento + 1)
        await asyncio.sleep(5)
    raise RuntimeError(f"DeepSeek contestó vacío dos veces ({origen})")


# ---------- carpetas / semestres ----------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def semestres() -> list[str]:
    found = [d.name for d in WORKSPACE.iterdir() if d.is_dir() and re.fullmatch(r"SEMESTRE_\d+", d.name)]
    return sorted(found, key=lambda n: int(n.split("_")[1]))


def semestre_actual() -> str:
    if SEMESTRE_FORZADO:
        return SEMESTRE_FORZADO
    s = semestres()
    return s[-1] if s else ""


def materias(sem: str | None = None) -> list[str]:
    sem = sem or semestre_actual()
    base = WORKSPACE / sem
    if not sem or not base.is_dir():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_", "Z99")) and d.name != "skills"
    )


def buscar_materia(nombre: str) -> str | None:
    n = norm(nombre)
    ms = materias()
    for m in ms:
        if norm(m) == n:
            return m
    candidatos = [m for m in ms if n in norm(m) or norm(m) in n]
    return candidatos[0] if len(candidatos) == 1 else None


def carpeta_trabajo(materia: str | None, parcial: str | None) -> str:
    """Ruta relativa a WORKSPACE donde trabajará Claude."""
    if not materia:
        return "OTROS"
    m = buscar_materia(materia)
    if not m:
        raise ValueError(f"No encontré la materia '{materia}'. Materias: {', '.join(materias())}")
    rel = f"{semestre_actual()}/{m}"
    if parcial:
        p = parcial.upper().replace(" ", "")
        if not re.fullmatch(r"P[1-3]", p):
            num = re.sub(r"\D", "", p)[:1]
            if num not in ("1", "2", "3"):
                raise ValueError(f"Parcial no válido: '{parcial}'. Usa P1, P2 o P3.")
            p = "P" + num
        rel += f"/{p}"
    return rel


# ---------- historial (SQLite) ----------

db = basedatos.conectar()  # aplica las migraciones pendientes (tablas, columnas, índices) — ver basedatos.py
memoria.db = db  # la memoria de largo plazo vive en la misma base


def kv_get(k: str, default: str = "") -> str:
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(k: str, v: str):
    db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?,?)", (k, v))
    db.commit()


def parcial_actual() -> str:
    return kv_get("parcial", os.environ.get("PARCIAL_ACTUAL", "P1"))


def tareas_pendientes() -> list[dict]:
    cols = ["id", "curso_id", "curso", "materia", "titulo", "descripcion", "link", "entrega", "carpeta",
            "estado", "avisos", "pospuesta", "evento_id", "creada"]
    rows = db.execute("SELECT * FROM tareas WHERE estado='pendiente' ORDER BY entrega IS NULL, entrega").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def tarea(tid: str) -> dict | None:
    for t in tareas_pendientes():
        if t["id"] == tid:
            return t
    row = db.execute("SELECT id, titulo, materia, curso, carpeta, descripcion, entrega, link FROM tareas WHERE id=?", (tid,)).fetchone()
    return dict(zip(["id", "titulo", "materia", "curso", "carpeta", "descripcion", "entrega", "link"], row)) if row else None


def add_msg(chat: int, role: str, content: str):
    db.execute("INSERT INTO msgs(chat, role, content, ts) VALUES (?,?,?,?)", (chat, role, content, time.time()))
    db.commit()


DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def history(chat: int) -> list[dict]:
    """Los últimos mensajes, con un separador cada que cambia el día: sin eso DeepSeek no sabe de cuándo es cada
    mensaje y se aferra a lo que dijo antes (un domingo juró todo el día que era lunes)."""
    rows = db.execute("SELECT role, content, ts FROM msgs WHERE chat=? ORDER BY id DESC LIMIT ?",
                      (chat, HISTORY_LEN)).fetchall()
    out: list[dict] = []
    dia_previo = None
    for r, c, ts in reversed(rows):
        d = datetime.fromtimestamp(ts or time.time(), jarvis.TZ).date()
        if d != dia_previo:
            out.append({"role": "system", "content": f"— Mensajes del {DIAS_SEMANA[d.weekday()]} {d:%Y-%m-%d} —"})
            dia_previo = d
        out.append({"role": r, "content": c})
    return out


def nota_hoy() -> dict:
    """Va AL FINAL de los mensajes (lo más cerca de la respuesta): la fecha real manda sobre lo que se dijo antes."""
    now = jarvis.ahora()
    return {"role": "system", "content": (
        f"[FECHA REAL: hoy es {DIAS_SEMANA[now.weekday()]} {now:%Y-%m-%d}, son las {now:%H:%M} (Guadalajara). "
        "Si antes en la conversación tú dijiste otro día, estabas equivocado: no lo repitas y, si viene al caso, "
        "corrígelo sin rodeos. Nunca discutas la fecha con Chava usando lo que dijiste antes.]")}


# ---------- DeepSeek ----------

_agenda_cache: dict = {"ts": 0.0, "texto": ""}


def invalidar_agenda():
    _agenda_cache["ts"] = 0.0


def agenda_contexto(now: datetime) -> str:
    """Agenda REAL de Google (ayer → +7 días) para meterla en el prompt: DeepSeek tiende a contestar de memoria
    en vez de consultar calendario_ver, y así siempre tiene los datos correctos a la vista."""
    if not g.conectadas():
        return ""
    if time.time() - _agenda_cache["ts"] < 180:
        return _agenda_cache["texto"]
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    try:
        evs = g.cal_listar((now - timedelta(days=1)).strftime("%Y-%m-%d"), (now + timedelta(days=7)).strftime("%Y-%m-%d"))
    except Exception as e:
        log.warning("agenda contexto: %s", e)
        return ""
    por_dia: dict[str, list[str]] = {}
    for e in evs:
        hora = "todo el día" if e["todo_el_dia"] else f"{e['inicio'][11:]}-{e['fin'][11:]}"
        por_dia.setdefault(e["inicio"][:10], []).append(
            f"  {hora} {e['titulo']}" + (f" @ {e['lugar']}" if e["lugar"] else "")
            + f"  [cal: {e['calendario'][:22]} · cuenta {e['cuenta']} · id {e['id']}"
            + (f" · serie {e['serie_id']}" if e.get("serie_id") else "") + "]")
    lineas = []
    for i in range(-1, 8):
        d = now + timedelta(days=i)
        etiqueta = {-1: "AYER", 0: "HOY", 1: "MAÑANA"}.get(i, "")
        lineas.append(f"{dias[d.weekday()]} {d:%Y-%m-%d} {etiqueta}".rstrip() + ":")
        lineas += por_dia.get(d.strftime("%Y-%m-%d"), ["  (nada)"])
    _agenda_cache.update(ts=time.time(), texto="\n".join(lineas))
    return _agenda_cache["texto"]


def tabla_fechas(now: datetime) -> str:
    """Días de los próximos 14 días ya resueltos: DeepSeek se equivoca calculando fechas relativas."""
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    lineas = []
    for i in range(-1, 14):
        d = now + timedelta(days=i)
        etiqueta = {-1: " (ayer)", 0: " (HOY)", 1: " (mañana)", 2: " (pasado mañana)"}.get(i, "")
        lineas.append(f"{dias[d.weekday()]} {d:%Y-%m-%d}{etiqueta}")
    proximos = []
    for k in range(1, 8):
        d = now + timedelta(days=k)
        proximos.append(f"'el {dias[d.weekday()]}' = {d:%Y-%m-%d}")
    return ("FECHAS (úsalas SIEMPRE, no calcules tú):\n" + " · ".join(lineas)
            + "\nCuando diga un día de la semana sin más, es el PRÓXIMO: " + ", ".join(proximos)
            + ". No preguntes '¿cuál lunes?'; solo si dice 'el otro lunes' o 'en dos semanas' usa el siguiente.")


def system_prompt(consulta: str = "") -> str:
    """Primero lo que casi no cambia (personalidad, perfil, reglas, habilidades, memoria) y AL FINAL lo que cambia cada
    minuto (hora, agenda): DeepSeek cachea el inicio del prompt cuando se repite igual, y eso abarata cada mensaje.
    `consulta`: lo último que escribió Chava, para traer los recuerdos que vienen al caso."""
    sem = semestre_actual()
    now = jarvis.ahora()
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    perfil = jarvis.perfil()
    al_caso = memoria.relevantes(consulta, con_perfil=bool(perfil)) if consulta else ""
    return f"""{jarvis.personalidad()}

# Quién es Chava (tu perfil de él; lo reescribes cada domingo)
{perfil or '(todavía sin perfil: usa tu memoria)'}

# Cómo trabajas
- Actúas DURANTE tu respuesta, con herramientas; después de mandar el mensaje ya no haces nada hasta que él escriba.
  Si algo se puede hacer, hazlo en este turno y contesta con el resultado. Si ninguna herramienta sirve, dilo claro.
  (Solo Claude, después de `delegar_a_claude`, y los recordatorios siguen trabajando solos.)
- Cuenta como hecho solo lo que una herramienta confirmó en este turno; si dio error, dile el error.
- Tú resuelves con tus herramientas rápidas: platicar, explicar, planear, fechas, cálculos, redactar, calendario,
  correo, Classroom, archivos, notas, memoria, recordatorios.
- Claude Code (servidor, con skills de Word/PDF/PowerPoint/Excel/diseño/video, web y código) es tu brazo para
  CREAR o EDITAR archivos e investigar a fondo. Tiene un límite escaso: máximo un `delegar_a_claude` por mensaje.
  Preguntas sobre ti, la hora o el sistema las contestas tú.

# Tus habilidades
Manuales detallados para cada tipo de trabajo. Cuando el mensaje cae en una, ábrela con `habilidad` ANTES de actuar
(puedes abrir varias; es rápido y gratis). Si Chava te enseña cómo quiere algo que se repite, guárdalo con
`habilidad_escribir`.
{habilidades.indice()}

# Cómo platicas por Telegram
- Primero la respuesta o el resultado; después, si hace falta, una sola pregunta. Mensajes cortos, como de WhatsApp.
- Preguntar bien: solo lo que cambia el resultado, todo junto en UN mensaje (máx. 3 cosas), con tu propuesta por
  default ("¿a color? si no, va en B/N"). Lo que puedas saber con tus herramientas o tu memoria, búscalo en vez de preguntar.
- Botones: existen solo si llamas `ofrecer_opciones`. Úsalos para elegir entre 2-4 opciones (sí/no, formato, planes),
  textos de ≤25 caracteres. Las respuestas abiertas (hora, lugar, nombres) que las escriba él.
- Al hacer algo, confírmalo en una línea con el dato clave (📅 fecha y hora · 📎 archivo · 🖨 copias).
- Formato: **negritas** e _itálicas_; listas cortas; sin tablas ni encabezados.
- Voz: si te mandó audio, tu respuesta sale como nota de voz; si pide audio por texto → `contestar_con_voz`.
  Cuando va a ser audio, escribe como hablas, sin listas ni links. Sí puedes mandar audio.

# Tu memoria y tu iniciativa
- Su PERFIL (arriba) y tu NÚCLEO (abajo) = lo esencial, siempre lo tienes. Al final vienen además los recuerdos que
  vienen al caso con lo que acaba de escribir. Lo demás está en tu memoria de largo plazo.
- Úsalo con naturalidad, como un compa que se acuerda ("¿qué onda con…?"), sin recitarle su ficha.
- Aprendes algo que vale la pena (gustos, planes, fechas, personas, cómo se siente) → `recordar` en ese momento, un
  dato por llamada, con fechas absolutas y `vigente_hasta` si caduca. Cambió → `recordar` con su #id; ya no aplica → `olvidar`.
- Antes de decir "no sé" o "no me acuerdo" → `recuerdos_buscar` y `platicas_buscar` (ves solo los últimos mensajes).
- Algo que va a pasar (examen, entrevista, cita, que se siente mal) → `seguimiento` para preguntarle después.
  Te pide que le recuerdes algo → `recordatorio_crear`. Tu libreta (`nota_*`) es tuya.
- Guarda en silencio: no anuncies cada dato que recuerdas.

{jarvis.contexto(memoria.nucleo(con_perfil=bool(perfil)), memoria.conteo())}

{contexto_google()}

# TU AGENDA REAL (Google Calendar, todas las cuentas, se actualiza sola; esto ES su calendario ahora mismo)
Contesta "¿qué tengo?", "¿qué clase sigue?", "¿estoy libre?" CON ESTO, sin inventar ni usar tu memoria.
Para crear/mover/borrar sí usa las herramientas (los id están aquí).
{agenda_contexto(now) or '(Google no conectado)'}

{contexto_eventos()}

# Ahora
Hablas con Chava por Telegram, en su iPhone. Ahora es {dias[now.weekday()]} {now:%Y-%m-%d %H:%M} (Guadalajara).
{tabla_fechas(now)}
Semestre activo: {sem or '(ninguno)'} · parcial actual: {parcial_actual()} (úsalo si no dice otro).
Materias (nombres exactos de carpeta, cada una con P1, P2, P3): {', '.join(materias()) or '(ninguna)'}
Estado: Claude {"PRENDIDO" if claude_activo() else "APAGADO (/claude off): no le puedes encargar nada"} · impresión {"PRENDIDA" if print_activo() else "APAGADA (/print off)"}.
""" + (f"""
# Recuerdos que vienen al caso con lo que acaba de escribir (de tu memoria de largo plazo)
{al_caso}
""" if al_caso else "")


async def refrescar_permisos():
    """Actualiza en un hilo la caché de permisos faltantes (hace red), para que system_prompt() no bloquee."""
    if g.configurado():
        for c in g.conectadas():
            await asyncio.to_thread(g.permisos_faltantes_cache, c)


def contexto_google() -> str:
    if not g.configurado() or not g.conectadas():
        return "# Google\n(No conectado todavía: si pide algo de calendario/correo/Classroom, dile que use /google.)"
    lineas = []
    for t in tareas_pendientes()[:15]:
        cuando = t["entrega"][:16].replace("T", " ") if t["entrega"] else "sin fecha"
        lineas.append(f"- [{t['id']}] {t['materia'] or t['curso']}: {t['titulo']} — entrega {cuando}")
    avisos = []
    for c in g.conectadas():
        if faltan := g.permisos_faltantes_guardados(c):
            avisos.append(f"⚠️ La cuenta '{c}' NO tiene permiso de: {', '.join(faltan)}. Con esa cuenta esas "
                          f"herramientas van a fallar: dile a Chava que mande /google {c} y marque TODAS las casillas.")
    return f"""# Google (cuentas conectadas: {', '.join(g.conectadas())}; calendario principal: {g.CUENTA_CALENDARIO})
{chr(10).join(avisos)}
Calendario, correo y Classroom: abre las habilidades `calendario`, `correo` y `classroom`.

# Tareas de Classroom pendientes (las revisas tú solo cada {CLASSROOM_CADA_MIN} min)
{chr(10).join(lineas) or '(ninguna)'}"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delegar_a_claude",
            "description": ("Manda una tarea PESADA a Claude Code (gasta su límite diario, que es escaso). Solo para crear/editar "
                            "archivos, hacer tareas, investigar a fondo o programar. NUNCA para preguntas, la hora, platicar o "
                            "revisar el sistema. Máximo una vez por mensaje."),
            "parameters": {
                "type": "object",
                "properties": {
                    "espacio": {"type": "string", "enum": ["escuela", "personal", "diseño"],
                                "description": "'escuela' = algo de una materia (va a su carpeta y parcial). 'personal' = lo que no es de la escuela: sus papeles (Documentos/Personal) o, si tipo es video/diseno/presentacion, Documentos/Creaciones. 'diseño' = atajo de personal + tipo diseno."},
                    "tipo": {"type": "string", "enum": ["tarea", "presentacion", "video", "diseno", "documento", "archivos", "otro"],
                             "description": "Qué se va a entregar: decide la carpeta, cuánto tiempo se le da y cómo se revisa y se manda. 'archivos' = acomodar/organizar sus archivos."},
                    "plan": {"type": "string", "description": "Solo video y presentacion: 3-4 líneas para Chava ANTES de arrancar: qué vas a hacer, duración o número de diapositivas, formato y estilo. Se le muestra con botones [Hazlo] [Cambiar]."},
                    "carpeta": {"type": "string", "description": "Solo espacio personal: subcarpeta de JARVIS/archivos donde trabajar, p. ej. 'PLEXUS'. Vacío = archivos/."},
                    "enviar_archivos": {"type": "boolean", "description": "Si al terminar le mando los archivos por Telegram. Default: sí en escuela, no en personal."},
                    "materia": {"type": "string", "description": "Nombre exacto de la carpeta de la materia. Vacío si no es escolar."},
                    "parcial": {"type": "string", "enum": ["P1", "P2", "P3", ""], "description": "Parcial. Vacío = el parcial actual."},
                    "prompt": {"type": "string", "description": "Instrucciones completas y autocontenidas para Claude."},
                    "titulo": {"type": "string", "description": "Título corto de la tarea (para avisos)."},
                    "continuar_sesion": {"type": "boolean", "description": "true = sigue en la MISMA sesión del último trabajo de Claude (recuerda todo lo que hizo, misma carpeta). Para corregir, terminar o continuar ese trabajo; el prompt solo dice qué falta. Ignora materia/parcial/carpeta."},
                },
                "required": ["prompt", "titulo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_archivos",
            "description": "Lista los archivos de una materia/parcial del semestre activo.",
            "parameters": {
                "type": "object",
                "properties": {"materia": {"type": "string"}, "parcial": {"type": "string"}},
                "required": ["materia"],
            },
        },
    },
]


TOOLS.append({
    "type": "function",
    "function": {
        "name": "ofrecer_opciones",
        "description": "Pone botones debajo de tu siguiente mensaje para que Chava elija con un toque. Después de llamarla, escribe tu mensaje (la pregunta).",
        "parameters": {
            "type": "object",
            "properties": {"opciones": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6}},
            "required": ["opciones"],
        },
    },
})


# ---------- archivos que llegan por Telegram (organizarlos lo hace Claude) ----------

def _entrada() -> Path:
    return WORKSPACE / ENTRADA_DIR


ARCHIVO_TOOLS = [
    {"type": "function", "function": {
        "name": "mandar_archivo",
        "description": ("Le manda a Chava por Telegram un archivo de sus carpetas (video, PDF, Word, imagen…). Sirve con "
                        "cualquier ruta que dé Claude: 'CETI COLOMOS/…', 'JARVIS/…', '/workspace/…' o '/jarvis/…'."),
        "parameters": {"type": "object", "properties": {"ruta": {"type": "string"}}, "required": ["ruta"]}}},
    {"type": "function", "function": {
        "name": "archivos_entrada",
        "description": "Lista los archivos que Chava te mandó por Telegram y que siguen en la bandeja de entrada.",
        "parameters": {"type": "object", "properties": {}}}},
]


def ejecutar_tool_archivos(nombre: str, args: dict) -> str | None:
    if nombre == "archivos_entrada":
        items = sorted(p.name for p in _entrada().glob("*") if p.is_file()) if _entrada().is_dir() else []
        return f"{len(items)} archivos en la entrada:\n" + "\n".join(items[-60:]) if items else "La entrada está vacía."
    return None


def _f(nombre: str, descripcion: str, props: dict, requeridos: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": nombre, "description": descripcion, "parameters": {
        "type": "object", "properties": props, **({"required": requeridos} if requeridos else {})}}}


S = {"type": "string"}
CUENTA = {"type": "string", "description": ("'escuela' (CETI) o 'personal' (su Gmail, tu-correo; es el MISMO que usa para buscar trabajo, "
                                         "así que 'empleo' = 'personal'). Vacío = todas cuando aplica.")}
FECHA = {"type": "string", "description": "'AAAA-MM-DD' o 'AAAA-MM-DD HH:MM' (hora de Guadalajara)."}
COLOR = {"type": "string", "description": "verde, verde claro, rojo, amarillo, naranja, morado, azul, azul claro, rosa, gris, lila."}
CAL_ID = {"type": "string", "description": "calendario_id que sale en calendario_ver (default 'primary')."}
ADJ = {"type": "array", "items": {"type": "string"},
       "description": "Rutas de archivos a adjuntar, p. ej. 'CETI COLOMOS/SEMESTRE_1/…/26310453_Tarea.pdf' o 'JARVIS/archivos/…'."}

GOOGLE_TOOLS = [
    # ----- Calendar -----
    _f("calendario_ver", "Eventos de TODOS sus calendarios (principal de ambas cuentas, tareas rojo/verde, festivos…) entre dos fechas.",
       {"desde": FECHA, "hasta": {"type": "string", "description": "'AAAA-MM-DD' (incluido) o 'AAAA-MM-DD HH:MM'."}}, ["desde", "hasta"]),
    _f("calendario_libre", "Bloques OCUPADOS entre dos fechas (todas las cuentas). Úsala para encontrar huecos libres o checar choques.",
       {"desde": FECHA, "hasta": FECHA}, ["desde", "hasta"]),
    _f("calendario_calendarios", "Lista sus calendarios (nombre, id, cuenta, color).", {}),
    _f("calendario_crear", "Crea un evento. Puede repetirse, tener invitados y link de Google Meet.",
       {"titulo": S, "inicio": FECHA, "fin": FECHA,
        "duracion_min": {"type": "integer", "description": "Si no hay fin. Default 60."},
        "descripcion": S, "lugar": S, "cuenta": CUENTA, "color": COLOR,
        "avisos_min": {"type": "array", "items": {"type": "integer"}, "description": "Minutos antes para notificar, p. ej. [30, 1440]."},
        "repetir": {"type": "string", "description": "'diario', 'semanal', 'semanal:lu,mi,vi', 'mensual', 'anual' o un 'RRULE:...'."},
        "repetir_hasta": {"type": "string", "description": "'AAAA-MM-DD' última fecha de la serie."},
        "repetir_veces": {"type": "integer", "description": "Número de repeticiones (si no hay repetir_hasta)."},
        "invitados": {"type": "array", "items": {"type": "string"}, "description": "Correos a invitar (les llega invitación)."},
        "meet": {"type": "boolean", "description": "true = agregar link de Google Meet."},
        "calendario_id": CAL_ID},
       ["titulo", "inicio"]),
    _f("horario_cargar", ("Carga/reemplaza TODO su horario de clases como eventos semanales en el calendario '🏫 Clases' "
                          "(borra lo anterior de ese calendario, sin duplicados). Úsala para el horario; nunca crees clases "
                          "como eventos sueltos. Junta en una sola clase las horas seguidas de la misma materia."),
       {"clases": {"type": "array", "items": {"type": "object", "properties": {
            "materia": S, "dia": {"type": "string", "enum": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]},
            "inicio": {"type": "string", "description": "HH:MM"}, "fin": {"type": "string", "description": "HH:MM"},
            "salon": S, "edificio": S, "profesor": S}, "required": ["materia", "dia", "inicio", "fin"]}},
        "desde": {"type": "string", "description": "AAAA-MM-DD desde cuándo (normalmente hoy)."},
        "hasta": {"type": "string", "description": "AAAA-MM-DD fin del semestre (AGO-DIC → mediados de diciembre)."}},
       ["clases", "desde", "hasta"]),
    _f("calendario_editar", "Mueve o cambia un evento. Para cambiar TODA una serie que se repite, usa su serie_id como evento_id.",
       {"cuenta": S, "evento_id": S, "calendario_id": CAL_ID, "titulo": S, "inicio": FECHA, "fin": FECHA,
        "descripcion": S, "lugar": S, "color": COLOR}, ["cuenta", "evento_id"]),
    _f("calendario_borrar", "Borra un evento (con el serie_id borra toda la serie).",
       {"cuenta": S, "evento_id": S, "calendario_id": CAL_ID}, ["cuenta", "evento_id"]),
    # ----- Gmail -----
    _f("gmail_buscar", ("Busca correos. Sintaxis Gmail: from:, to:, subject:, has:attachment, filename:pdf, is:unread, "
                        "is:starred, label:, newer_than:7d, after:2026/09/01, before:, category:primary. "
                        "OJO: distingue acentos ('nomina' ≠ 'nómina')."),
       {"consulta": S, "cuenta": CUENTA, "maximo": {"type": "integer", "description": "Máx. 25."}}),
    _f("gmail_leer", "Lee un correo completo (cuerpo, adjuntos, cc).", {"cuenta": S, "id": S}, ["cuenta", "id"]),
    _f("gmail_hilo", "Lee toda la conversación (hilo) de un correo.", {"cuenta": S, "hilo": S}, ["cuenta", "hilo"]),
    _f("gmail_marcar", "Marca/organiza correos.",
       {"cuenta": S, "ids": {"type": "array", "items": {"type": "string"}},
        "accion": {"type": "string", "description": ("leido | no_leido | archivar | desarchivar | destacar | quitar_destacado | "
                                                     "importante | papelera | etiqueta:<nombre> | quitar_etiqueta:<nombre>")}},
       ["cuenta", "ids", "accion"]),
    _f("gmail_etiquetas", "Lista las etiquetas de una cuenta.", {"cuenta": S}, ["cuenta"]),
    _f("gmail_enviar", "Prepara un correo (con adjuntos opcionales). Chava lo ve y lo confirma con un botón antes de que salga.",
       {"cuenta": {"type": "string", "description": "Desde qué cuenta: 'escuela' para profes, 'personal' para lo demás."},
        "para": S, "cc": S, "asunto": S, "cuerpo": S, "adjuntos": ADJ,
        "responder_a": {"type": "string", "description": "id del correo al que responde (mantiene el hilo)."}},
       ["cuenta", "para", "asunto", "cuerpo"]),
    _f("gmail_borrador", "Deja un borrador guardado en su Gmail (no se envía).",
       {"cuenta": S, "para": S, "cc": S, "asunto": S, "cuerpo": S, "adjuntos": ADJ, "responder_a": S},
       ["cuenta", "para", "asunto", "cuerpo"]),
    _f("gmail_guardar_adjuntos",
       ("Busca TODOS los correos que coinciden (sin límite de 25) y guarda sus adjuntos en JARVIS/archivos/<carpeta> "
        "(se ve en Samba). Si un correo no trae adjuntos guarda su contenido."),
       {"consulta": {"type": "string", "description": "Pruébala antes con gmail_buscar; si da 0 intenta con/sin acentos o por remitente."},
        "carpeta": S, "cuenta": CUENTA}, ["consulta", "carpeta"]),
    _f("archivos_listar", "Lista lo que hay en JARVIS/archivos o una subcarpeta.", {"carpeta": S}),
    # ----- Classroom -----
    _f("classroom_cursos", "Sus clases activas de Classroom (id, nombre, sección).", {}),
    _f("classroom_tareas", ("Tareas de una clase (o de todas) con SU estado (NEW/CREATED = sin entregar, TURNED_IN = entregada, "
                            "RETURNED = calificada/devuelta), si fue tarde, calificación, puntos máx. y tema/parcial."),
       {"curso": {"type": "string", "description": "Nombre o id de la clase. Vacío = todas."},
        "filtro": {"type": "string", "enum": ["pendientes", "entregadas", "calificadas", "todas"]}}),
    _f("classroom_anuncios", "Últimos anuncios/avisos del profe en una clase.",
       {"curso": S, "n": {"type": "integer"}}, ["curso"]),
    _f("classroom_materiales", "Material de clase (apuntes, presentaciones, links) que subió el profe.",
       {"curso": S, "n": {"type": "integer"}}, ["curso"]),
    _f("classroom_profesores", "Nombre y correo del/los profesores de una clase (para escribirles).",
       {"curso": S}, ["curso"]),
    _f("classroom_ignorar", ("Deja de revisar/avisar las tareas de una clase (materia exentada, que dio de baja o que ya no lleva), "
                             "o vuelve a revisarla. Borra sus pendientes y sus eventos rojos."),
       {"curso": {"type": "string", "description": "Nombre de la clase o de la materia."},
        "ignorar": {"type": "boolean", "description": "true = ignorar, false = volver a revisar."}}, ["curso", "ignorar"]),
    _f("tarea_marcar_hecha", "Marca una tarea de Classroom como hecha: deja de insistirle y su evento pasa al calendario verde (🟢).",
       {"id": S}, ["id"]),
]


# Nombre interno (el que usan el worker y las herramientas) → como Chava lo ve en Samba. El orden importa.
SAMBA = (("JARVIS/archivos/", "Documentos/Personal/"), ("JARVIS/", "KORRA/"), ("CETI COLOMOS/", "Documentos/Escuela/"),
         ("Creaciones/", "Documentos/Creaciones/"))


def a_samba(rel: str) -> str:
    for interno, samba in SAMBA:
        if rel.startswith(interno):
            return samba + rel[len(interno):]
    return rel


def resolver_ruta(texto: str) -> Path:
    """'CETI COLOMOS/…' o 'JARVIS/…', su nombre en Samba (o relativo a alguno) → ruta real, sin salirse de esas carpetas."""
    t = texto.strip().strip("`'\"").lstrip("/")
    for interna, visible in (("workspace/", "CETI COLOMOS/"), ("jarvis/", "JARVIS/"), ("creaciones/", "Creaciones/")):
        if t.startswith(interna):
            t = visible + t[len(interna):]
    for interno, samba in SAMBA:
        if t.startswith(samba):
            t = interno + t[len(samba):]
            break
    for prefijo, base in (("CETI COLOMOS/", WORKSPACE), ("JARVIS/", jarvis.BASE), ("Creaciones/", CREACIONES),
                          ("", WORKSPACE), ("", jarvis.BASE), ("", CREACIONES)):
        if prefijo and not t.startswith(prefijo):
            continue
        p = (base / t[len(prefijo):]).resolve()
        if p.is_relative_to(base.resolve()) and p.is_file():
            return p
    raise ValueError(f"No encontré el archivo: {texto}")


def rutas_visibles(texto: str) -> str:
    """Claude escribe rutas de su contenedor; se muestran como las carpetas que Chava ve en Samba."""
    for a, b in (("/workspace/", "Documentos/Escuela/"), ("/jarvis/archivos/", "Documentos/Personal/"),
                 ("/jarvis/", "KORRA/"), ("/creaciones/", "Documentos/Creaciones/"), ("CETI COLOMOS/", "Documentos/Escuela/"),
                 ("JARVIS/archivos/", "Documentos/Personal/"), ("JARVIS/", "KORRA/")):
        texto = texto.replace(a, b)
    return texto


VIDEOS = {".mp4", ".mov", ".m4v"}
IMAGENES = {".png", ".jpg", ".jpeg", ".webp"}


async def mandar_archivo_tg(bot, chat: int, p: Path, caption: str = "", reply_markup=None):
    """Manda un archivo por Telegram; los videos van como video (se ven y se guardan directo en el iPhone)."""
    tiempos = {"write_timeout": 300, "read_timeout": 120}  # subir decenas de MB desde la Pi tarda
    with p.open("rb") as fh:
        if p.suffix.lower() in IMAGENES and p.stat().st_size < 10 * 1024 * 1024:  # límite de fotos en Telegram
            # La foto se ve al instante pero Telegram la recomprime: el original va además como documento.
            await bot.send_photo(chat, fh, caption=caption[:1000], **tiempos)
            fh.seek(0)
            await bot.send_document(chat, fh, filename=p.name, caption="Original sin comprimir",
                                    reply_markup=reply_markup, **tiempos)
        elif p.suffix.lower() in VIDEOS:
            await bot.send_video(chat, fh, filename=p.name, caption=caption[:1000], supports_streaming=True,
                                 reply_markup=reply_markup, **tiempos)
        else:
            await bot.send_document(chat, fh, filename=p.name, caption=caption[:1000], reply_markup=reply_markup, **tiempos)


async def tool_mandar_archivo(ctx: ContextTypes.DEFAULT_TYPE, chat: int, a: dict) -> str:
    try:
        p = resolver_ruta(a.get("ruta", ""))
    except ValueError as e:
        return str(e)
    if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        return f"Pesa {p.stat().st_size / 1048576:.0f} MB y Telegram solo deja {MAX_FILE_MB}: dile que lo abra desde Samba en la Mac."
    await mandar_archivo_tg(ctx.bot, chat, p, reply_markup=boton_imprimir(ctx, p))
    return f"Enviado por Telegram: {p.name}"


async def _curso(nombre: str) -> dict:
    cursos = await asyncio.to_thread(g.classroom_cursos)
    n = norm(nombre or "")
    for c in cursos:
        if c["id"] == nombre or norm(c["nombre"]) == n:
            return c
    materia = buscar_materia(nombre) if n else None
    candidatos = [c for c in cursos if n and (n in norm(c["nombre"]) or
                                              (materia is not None and mapear_materia(c["nombre"]) == materia))]
    if len(candidatos) == 1:
        return candidatos[0]
    if candidatos:
        raise ValueError(f"'{nombre}' coincide con varias clases: {', '.join(c['nombre'] for c in candidatos)}. Especifica cuál.")
    raise ValueError(f"No encontré la clase '{nombre}'. Clases: {', '.join(c['nombre'] for c in cursos)}")


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False) if x else "(nada)"


async def pedir_confirmacion(ctx: ContextTypes.DEFAULT_TYPE, chat: int, pregunta: str, accion: dict) -> str:
    """Acciones destructivas (borrar eventos, papelera): el contenido de correos y Classroom llega a
    DeepSeek y podría inducirlo a borrar cosas, así que Chava las confirma con un botón."""
    key = uuid.uuid4().hex[:8]
    ctx.bot_data.setdefault("confirmar", {})[key] = accion
    await ctx.bot.send_message(chat, pregunta[:4000], reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data=f"conf:ok:{key}"),
        InlineKeyboardButton("❌ No", callback_data=f"conf:no:{key}")]]))
    return "Le mostré a Chava botones para confirmar. Aún NO se hace; no digas que ya quedó."


async def ejecutar_confirmada(accion: dict) -> str:
    a = accion["args"]
    if accion["tipo"] == "cal_borrar":
        await asyncio.to_thread(g.cal_borrar, a["cuenta"], a["evento_id"], a.get("calendario_id", "primary"))
        return "Evento borrado"
    if accion["tipo"] == "horario":
        r = await asyncio.to_thread(g.horario_cargar, a["clases"], a["desde"], a["hasta"])
        invalidar_agenda()
        return f"Horario cargado: {len(r['clases_creadas'])} clases semanales en 🏫 Clases hasta {r['hasta']}"
    if accion["tipo"] == "papelera":
        return await asyncio.to_thread(g.gmail_marcar, a["cuenta"], a["ids"], "papelera")
    raise ValueError(f"acción desconocida: {accion['tipo']}")


async def ejecutar_tool_google(ctx: ContextTypes.DEFAULT_TYPE, chat: int, nombre: str, args: dict) -> str | None:
    """None = no es herramienta de Google."""
    a = {k: v for k, v in args.items() if v not in (None, "", [])}
    t = asyncio.to_thread
    if nombre == "tarea_marcar_hecha":
        await marcar_tarea_hecha(a["id"])
        return "Marcada como hecha; su evento pasó al calendario verde con ✅."
    if nombre == "gmail_guardar_adjuntos":
        partes = [re.sub(r'[\\:*?"<>|]', "_", x).strip() for x in a["carpeta"].split("/") if x.strip() not in ("", ".", "..")]
        destino = jarvis.BASE / "archivos" / Path(*partes) if partes else jarvis.BASE / "archivos" / "correos"
        await ctx.bot.send_chat_action(chat, ChatAction.UPLOAD_DOCUMENT)
        r = await t(g.gmail_guardar_adjuntos, a["consulta"], destino, a.get("cuenta", ""))
        r["carpeta"] = "JARVIS/" + str(destino.relative_to(jarvis.BASE))
        return _j(r)
    if nombre == "archivos_listar":
        base = jarvis.BASE / "archivos" / (a.get("carpeta") or "")
        if not base.resolve().is_relative_to((jarvis.BASE / "archivos").resolve()) or not base.is_dir():
            return "Esa carpeta no existe."
        items = sorted(str(p.relative_to(base)) + ("/" if p.is_dir() else "") for p in base.iterdir())
        return f"{len(items)} elementos:\n" + "\n".join(items[:80])
    # Calendar
    if nombre == "calendario_ver":
        return _j(await t(g.cal_listar, a["desde"], a["hasta"]))
    if nombre in ("calendario_crear", "calendario_editar", "calendario_borrar", "horario_cargar", "tarea_marcar_hecha"):
        invalidar_agenda()
    if nombre == "horario_cargar":
        # Reemplaza TODO el calendario de clases: siempre con confirmación.
        resumen = "\n".join(f"• {c['dia']} {c['inicio']}-{c['fin']} {c['materia']}" for c in a["clases"])
        return await pedir_confirmacion(ctx, chat, f"🏫 ¿Reemplazo tu horario de clases (hasta {a['hasta']}) por este?\n\n{resumen}",
                                        {"tipo": "horario", "args": a})
    if nombre == "calendario_libre":
        return "Ocupado: " + _j(await t(g.cal_ocupado, a["desde"], a["hasta"]))
    if nombre == "calendario_calendarios":
        return _j(await t(g.cal_calendarios))
    if nombre == "calendario_crear":
        return "Evento creado: " + _j(await t(g.cal_crear, **a))
    if nombre == "calendario_editar":
        return "Evento actualizado: " + _j(await t(g.cal_editar, **a))
    if nombre == "calendario_borrar":
        try:  # el título ayuda a Chava a saber qué confirma
            ev = await t(g.cal_obtener, a["cuenta"], a["evento_id"], a.get("calendario_id", "primary"))
            desc = f"«{ev['titulo']}» ({ev['inicio']})"
        except Exception:
            desc = f"id {a['evento_id']}"
        return await pedir_confirmacion(ctx, chat, f"🗑 ¿Borro el evento {desc} de «{a['cuenta']}»?",
                                        {"tipo": "cal_borrar", "args": a})
    # Gmail
    if nombre == "gmail_buscar":
        r = await t(g.gmail_buscar, a.get("consulta") or "is:unread newer_than:2d category:primary",
                    a.get("cuenta", ""), int(a.get("maximo") or 10))
        return _j(r) if r else "No hay correos con esa búsqueda."
    if nombre == "gmail_leer":
        return _j(await t(g.gmail_leer, a["cuenta"], a["id"]))
    if nombre == "gmail_hilo":
        return _j(await t(g.gmail_hilo, a["cuenta"], a["hilo"]))
    if nombre == "gmail_marcar":
        if a["accion"] == "papelera":
            return await pedir_confirmacion(ctx, chat, f"🗑 ¿Mando {len(a['ids'])} correo(s) de «{a['cuenta']}» a la papelera?",
                                            {"tipo": "papelera", "args": a})
        return await t(g.gmail_marcar, a["cuenta"], a["ids"], a["accion"])
    if nombre == "gmail_etiquetas":
        return _j(await t(g.gmail_etiquetas, a["cuenta"]))
    if nombre in ("gmail_enviar", "gmail_borrador"):
        adjuntos = [resolver_ruta(x) for x in a.get("adjuntos", [])]  # falla ya si alguna ruta no existe
        if nombre == "gmail_borrador":
            await t(g.gmail_borrador, a["cuenta"], a["para"], a["asunto"], a["cuerpo"], a.get("responder_a", ""),
                    adjuntos, a.get("cc", ""))
            return "Borrador guardado en su Gmail (no se envió)."
        key = uuid.uuid4().hex[:8]
        ctx.bot_data.setdefault("correos", {})[key] = a | {"adjuntos": [str(p) for p in adjuntos]}
        vista = (f"✉️ Correo por enviar ({a['cuenta']})\nPara: {a['para']}" + (f"\nCC: {a['cc']}" if a.get("cc") else "")
                 + f"\nAsunto: {a['asunto']}" + (f"\n📎 {', '.join(p.name for p in adjuntos)}" if adjuntos else "")
                 + f"\n\n{a['cuerpo']}")
        await ctx.bot.send_message(chat, vista[:4000], reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Enviar", callback_data=f"mail:send:{key}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"mail:cancel:{key}")]]))
        return "Correo mostrado a Chava con botones [Enviar] [Cancelar]. Aún NO se envía."
    # Classroom
    if nombre == "classroom_ignorar":
        return await ignorar_curso(a["curso"], a.get("ignorar", True))
    if nombre == "classroom_cursos":
        return _j(await t(g.classroom_cursos))
    if nombre == "classroom_tareas":
        cursos = [await _curso(a["curso"])] if a.get("curso") else await t(g.classroom_cursos)
        filtro = a.get("filtro", "pendientes")
        res = []
        for c in cursos:
            for x in await t(g.classroom_mis_tareas, c["id"], c["nombre"]):
                if (filtro == "pendientes" and x["estado"] in ("NEW", "CREATED", "RECLAIMED_BY_STUDENT", "")) or \
                   (filtro == "entregadas" and x["estado"] in ("TURNED_IN", "RETURNED")) or \
                   (filtro == "calificadas" and x["calificacion"] is not None) or filtro == "todas":
                    res.append(x)
        return _j(res[:60])
    if nombre == "classroom_anuncios":
        c = await _curso(a["curso"])
        return _j(await t(g.classroom_anuncios, c["id"], int(a.get("n") or 5)))
    if nombre == "classroom_materiales":
        c = await _curso(a["curso"])
        return _j(await t(g.classroom_materiales, c["id"], int(a.get("n") or 10)))
    if nombre == "classroom_profesores":
        c = await _curso(a["curso"])
        return _j(await t(g.classroom_profesores, c["id"]))
    return None


TOOLS.append({
    "type": "function",
    "function": {
        "name": "contestar_con_voz",
        "description": "Hace que tu siguiente respuesta le llegue a Chava como nota de voz. Úsala si te pide audio/voz.",
        "parameters": {"type": "object", "properties": {}},
    },
})


def a_html(texto: str) -> str:
    """Markdown sencillo de DeepSeek (**negrita**, _itálica_, `código`) → HTML de Telegram."""
    t = html.escape(texto, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    return t


async def enviar(bot, chat: int, texto: str, reply_markup=None, voz_ogg: bytes | None = None):
    """Manda la respuesta en UN solo mensaje cuando se puede: nota de voz con el texto como pie,
    o texto con formato. Si el formato falla, cae a texto plano."""
    if voz_ogg and len(texto) <= 1000:
        try:
            return await bot.send_voice(chat, voz_ogg, caption=a_html(texto), parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            return await bot.send_voice(chat, voz_ogg, caption=texto, reply_markup=reply_markup)
    if voz_ogg:
        await bot.send_voice(chat, voz_ogg)
    partes = [texto[i:i + 4000] for i in range(0, len(texto), 4000)]
    for i, parte in enumerate(partes):
        markup = reply_markup if i == len(partes) - 1 else None
        try:
            await bot.send_message(chat, a_html(parte), parse_mode="HTML", reply_markup=markup)
        except Exception:
            await bot.send_message(chat, parte, reply_markup=markup)


def teclado_opciones(ctx: ContextTypes.DEFAULT_TYPE, opciones: list[str]) -> InlineKeyboardMarkup:
    """Los textos se guardan en bot_data porque callback_data solo admite 64 bytes."""
    key = uuid.uuid4().hex[:8]
    ctx.bot_data.setdefault("opciones", {})[key] = opciones
    filas, fila = [], []
    for i, op in enumerate(opciones):
        fila.append(InlineKeyboardButton(op[:40], callback_data=f"op:{key}:{i}"))
        if len(fila) == 2 or len(op) > 18:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    return InlineKeyboardMarkup(filas)


def teclado_job_corriendo(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancelar", callback_data=f"job:cancel:{job_id}")]])


def teclado_job_terminado(job_id: str, job: dict | None = None) -> InlineKeyboardMarkup:
    filas = [[InlineKeyboardButton("🔁 Rehacer", callback_data=f"job:redo:{job_id}"),
              InlineKeyboardButton("✏️ Corregir algo", callback_data=f"job:fix:{job_id}")],
             [InlineKeyboardButton("➡️ Continuar esta sesión", callback_data=f"job:cont:{job_id}")]]
    if (job or {}).get("error_tipo") == "limite":
        filas.insert(0, [InlineKeyboardButton("⏰ Reintentar en 1 hora", callback_data=f"job:later:{job_id}")])
    return InlineKeyboardMarkup(filas)


def tool_listar(materia: str, parcial: str = "") -> str:
    rel = carpeta_trabajo(materia, parcial)
    base = WORKSPACE / rel
    if not base.is_dir():
        return f"{rel}: (no existe)"
    items = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.name.startswith((".", "~$")):
            items.append(str(p.relative_to(base)))
        if len(items) >= 60:
            items.append("…")
            break
    return f"{rel}:\n" + ("\n".join(items) or "(vacía)")


async def tool_recordatorio(app: Application, args: dict) -> str:
    item = jarvis.recordatorio_crear(args["cuando"], args["texto"])
    programar_recordatorio(app, item)
    return f"Recordatorio {item['id']} programado para {args['cuando']}."


def ejecutar_tool_jarvis(nombre: str, args: dict) -> str | None:
    """Herramientas del espacio propio que no necesitan Telegram. None = no es de aquí."""
    if (r := memoria.ejecutar(nombre, args)) is not None:
        return r
    if (r := habilidades.ejecutar(nombre, args)) is not None:
        return r
    if nombre == "seguimiento":
        return programar_seguimiento(args["texto"], args["cuando"])
    if nombre == "nota_escribir":
        return jarvis.nota_escribir(args["nombre"], args["contenido"], args.get("modo", "reemplazar"))
    if nombre == "nota_leer":
        return jarvis.nota_leer(args["nombre"])
    if nombre == "nota_borrar":
        return jarvis.nota_borrar(args["nombre"])
    if nombre == "recordatorio_borrar":
        return "Cancelado." if jarvis.recordatorio_borrar(args["id"]) else "No existe ese id."
    return None


SKILLS_DISENO = os.environ.get("SKILLS_DISENO",
    "/home/usuario/.claude/plugins/marketplaces/ui-ux-pro-max-skill/.claude/skills")
GUIA_DISENO = f"""Es un DISEÑO GRÁFICO para compartir por WhatsApp/Instagram, y debe verse PROFESIONAL y llamativo.
Cómo trabajar:
1. Antes de diseñar lee {SKILLS_DISENO}/banner-design/SKILL.md (y si sirve {SKILLS_DISENO}/design/SKILL.md) y sigue su método:
   dirección de arte clara, jerarquía tipográfica fuerte, paleta con contraste, composición con aire.
2. Hazlo en HTML + CSS (Google Fonts permitido; ilustraciones con SVG/CSS/emoji grandes; nada de imágenes de bancos
   con marca de agua) y expórtalo a PNG con Chromium headless al tamaño exacto, con escala 2 para nitidez:
   chromium --headless --no-sandbox --hide-scrollbars --force-device-scale-factor=2 --window-size=ANCHO,ALTO --screenshot=salida.png archivo.html
   Formatos: vertical/historia = 1080x1920 · post cuadrado = 1080x1080 · horizontal = 1920x1080 · carta = 816x1056.
3. MIRA el PNG que generaste (léelo como imagen) y corrige: texto cortado o encimado, poco contraste, márgenes,
   faltas de ortografía, datos equivocados. Itera hasta que se vea de diseñador, no de plantilla.
4. Entrega: el PNG final + el .html fuente. Si te piden "opciones", haz 2 variantes con estilos distintos.
5. Datos que no te dieron (lugar, hora…): NO los inventes; deja un espacio elegante tipo "📍 Lugar por confirmar".
"""


CARPETA_TIPO = {"video": "Videos", "diseno": "Diseños", "presentacion": "Presentaciones"}
TIMEOUT_TIPO = {"video": 2400, "presentacion": 1800}  # segundos; lo demás usa el del worker (20 min)
CONFIRMAR_TIPOS = ("video", "presentacion")  # gastan mucho límite: primero se le enseña el plan con botones


def destino_job(args: dict) -> tuple[str, str, bool]:
    """(carpeta de la API del worker, cómo se le dice a Claude, ¿es de la escuela?) según espacio, tipo y materia."""
    tipo = args.get("tipo") or ("diseno" if args.get("espacio") == "diseño" else "")
    escuela = args.get("espacio") == "escuela" or bool(args.get("materia"))
    if escuela:
        rel = carpeta_trabajo(args.get("materia"), args.get("parcial") or parcial_actual())
        return rel, f"Documentos/Escuela/{rel} (/workspace/{rel})", True
    if tipo in CARPETA_TIPO:
        nombre = re.sub(r'[\\/:*?"<>|]', "", args.get("titulo") or tipo).strip()[:50] or tipo
        sub = f"{CARPETA_TIPO[tipo]}/{jarvis.ahora():%Y-%m-%d} {nombre}"
        return f"Creaciones/{sub}", f"Documentos/Creaciones/{sub} (/creaciones/{sub})", False
    sub = "/".join(x for x in (args.get("carpeta") or "").split("/") if x.strip() not in ("", ".", ".."))
    return ("JARVIS/archivos" + (f"/{sub}" if sub else ""),
            f"Documentos/Personal{'/' + sub if sub else ''} (/jarvis/archivos{'/' + sub if sub else ''})", False)


async def tool_delegar(update: Update, ctx: ContextTypes.DEFAULT_TYPE, args: dict) -> str:
    tipo = args.get("tipo") or ("diseno" if args.get("espacio") == "diseño" else "otro")
    args = args | {"tipo": tipo}
    titulo = args.get("titulo") or "tarea"
    chat = update.effective_chat.id
    if tipo in CONFIRMAR_TIPOS and not args.get("confirmado") and not args.get("continuar_sesion"):
        pid = uuid.uuid4().hex[:8]
        kv_set(f"pedido:{pid}", json.dumps(args, ensure_ascii=False))
        rel, visible, _ = destino_job(args)
        plan = (args.get("plan") or "").strip() or args["prompt"][:400]
        await ctx.bot.send_message(
            chat, f"📋 <b>Plan: {html.escape(titulo)}</b>\n{html.escape(plan)}\n\n📁 {html.escape(visible.split(' (')[0])}\n"
                  f"⏱ Suele tardar {'10-20' if tipo == 'video' else '5-10'} min.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Hazlo", callback_data=f"pln:go:{pid}"),
                InlineKeyboardButton("✏️ Cambiar", callback_data=f"pln:edit:{pid}")]]))
        return ("Le mostré el plan con botones [Hazlo] [Cambiar]; NO se ha lanzado todavía. Dile en una línea que lo "
                "revise y le pique a Hazlo, sin repetir el plan.")
    enviar = args.get("enviar_archivos")
    if args.get("continuar_sesion") and (sid := kv_get("ultima_sesion")):
        try:
            job_id = await lanzar_job(ctx, chat, args["prompt"], "", titulo,
                                      enviar=True if enviar is None else bool(enviar), resume=sid, tipo=tipo)
            if not job_id:
                return CLAUDE_APAGADO
            return (f"Tarea enviada a Claude (id {job_id}) continuando su última sesión. Aún no termina; "
                    "el resultado llegará solo.")
        except LookupError:
            pass  # esa sesión ya no existe: se lanza como trabajo nuevo
    rel, visible, escuela = destino_job(args)
    prompt = args["prompt"]
    if tipo == "diseno":
        prompt = GUIA_DISENO + "\nPEDIDO:\n" + prompt
    prompt = (f"Carpeta de trabajo: {visible}. Ahí van los archivos FINALES (el código del proyecto en _proyecto/).\n"
              f"Tipo de entrega: {tipo}.\n\n{prompt}")
    personal = rel.startswith("JARVIS/")
    job_id = await lanzar_job(ctx, chat, prompt, rel, titulo,
                              enviar=(not personal) if enviar is None else bool(enviar), tipo=tipo, escuela=escuela)
    if not job_id:
        return ("Claude está APAGADO por Chava (/claude off): NO se lanzó nada. Dile que para esto necesita "
                "prenderlo con /claude on; mientras, ayúdale tú con lo que puedas sin Claude.")
    return f"Tarea enviada a Claude (id {job_id}) en {visible.split(' (')[0]}. Aún no termina; el resultado llegará solo."


def claude_activo() -> bool:
    """Interruptor /claude on|off: apagado, nada puede gastar el límite del plan de Claude."""
    return kv_get("claude_activo", "1") == "1"


CLAUDE_APAGADO = "🔌 Claude está apagado, no se lanzó nada. Préndelo con /claude on."


MAX_JOBS_INFO = 40


def jobs_info() -> dict[str, dict]:
    """Datos de cada trabajo lanzado (título, carpeta, prompt, si se mandan los archivos…). Viven en la base del bot,
    así que sobreviven a un reinicio: los botones Rehacer/Corregir y el seguimiento siguen funcionando."""
    return json.loads(kv_get("jobs_info", "{}") or "{}")


def guardar_job_info(job_id: str, **datos):
    todos = jobs_info()
    todos[job_id] = todos.get(job_id, {}) | datos
    kv_set("jobs_info", json.dumps(dict(list(todos.items())[-MAX_JOBS_INFO:]), ensure_ascii=False))


async def lanzar_job(ctx: ContextTypes.DEFAULT_TYPE, chat: int, prompt: str, rel: str, titulo: str,
                     enviar: bool = True, resume: str = "", tipo: str = "otro", escuela: bool = False) -> str:
    """Devuelve el id del trabajo, o "" si Claude está apagado (ya le avisó a Chava).
    `resume` = id de una sesión pasada: Claude la continúa en su carpeta (LookupError si ya no existe)."""
    if not claude_activo():
        await ctx.bot.send_message(chat, CLAUDE_APAGADO)
        return ""
    r = await worker.post("/jobs", json={"prompt": prompt, "cwd": rel, "resume": resume, "escuela": escuela,
                                         "timeout_s": TIMEOUT_TIPO.get(tipo, 0)})
    if resume and r.status_code == 404:
        raise LookupError("esa sesión ya no existe")
    r.raise_for_status()
    job = r.json()
    rel = job.get("cwd", rel)
    if resume and rel.startswith("JARVIS"):
        enviar = False  # espacio personal: no se mandan los archivos por Telegram
    cola = f" (en cola, posición {job['position']})" if job.get("position", 1) > 1 else ""
    aviso = await ctx.bot.send_message(
        chat, f"⏳ Claude está trabajando en «{titulo}»{cola}\n📁 {nombre_visible(rel) if rel else 'Documentos/Escuela'}"
              f"\n🆔 {job['id']}", reply_markup=teclado_job_corriendo(job["id"]))
    guardar_job_info(job["id"], prompt=prompt, cwd=rel, titulo=titulo, enviar=enviar, resume=resume, tipo=tipo,
                     escuela=escuela, chat=chat, aviso=aviso.message_id, pendiente=True)
    ctx.application.create_task(esperar_job(ctx, chat, job["id"], titulo, aviso.message_id))
    return job["id"]


async def job_reintento(ctx: ContextTypes.DEFAULT_TYPE):
    """Botón ⏰: relanza un trabajo que se topó con el límite de Claude."""
    info = jobs_info().get(ctx.job.data)
    if info:
        await lanzar_job(ctx, info.get("chat", ALLOWED_CHAT_ID), info["prompt"], info["cwd"],
                         f"{info.get('titulo', 'tarea')} (reintento)", enviar=info.get("enviar", True),
                         tipo=info.get("tipo", "otro"), escuela=info.get("escuela", False))


async def retomar_jobs(app: Application):
    """Al arrancar el bot: vuelve a seguir los trabajos que quedaron corriendo (antes se perdían; bug 8)."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(bot=app.bot, bot_data=app.bot_data, application=app)
    for job_id, info in jobs_info().items():
        if info.get("pendiente"):
            log.info("Retomo el seguimiento del trabajo %s", job_id)
            app.create_task(esperar_job(ctx, info.get("chat", ALLOWED_CHAT_ID), job_id, info.get("titulo", "tarea"),
                                        info.get("aviso")))


def herramientas() -> list[dict]:
    return TOOLS + MIS_TOOLS + habilidades.TOOLS + memoria.TOOLS + remoto.TOOLS + INICIATIVA_TOOLS + IMPRESORA_TOOLS + jarvis.TOOLS + ARCHIVO_TOOLS + (GOOGLE_TOOLS if g.configurado() and g.conectadas() else [])


AFIRMA_HECHO = re.compile(
    r"(ya (te )?(qued[oó]|quedaron|lo (puse|agend[eé]|anot[eé]|borr[eé]|mov[ií]|mand[eé]|envi[eé]))|"
    r"(agend[eé]|anot[eé]|apunt[eé]|borr[eé]|cre[eé]|mov[ií]|envi[eé]|guard[eé]|archiv[eé])\b.{0,40}"
    r"(calendario|evento|correo|clase|recordatorio|tarea|carpeta)|"
    r"(le )?(mand[eé]|pas[eé]|ped[ií]|encargu[eé]|dej[eé])\b.{0,40}\bclaude\b|claude ya (est[aá]|lo tiene)|"
    r"ya (se )?(imprimi[oó]|est[aá] imprimiendo)|(lo |la )?mand[eé] a imprimir|ya (lo |la )?imprim[ií])", re.I)


PROMESA = re.compile(
    r"d[eé]jame (buscar|revisar|checar|confirmar|ver|probar|jalar)|dame (un )?(momento|chance|tantito|sec)|"
    r"ahorita (mismo )?(lo |le |te )?(busco|hago|reviso|checo|entro|jalo|digo)|te aviso (c[oó]mo|cuando|qu[eé])|"
    r"d[eé]jame (lo )?(checo|reviso|veo|busco)|ya (lo )?(reviso|checo)|te digo en un (momento|rato)|"
    r"(le )?voy a (pedir|mandar|pasar|encargar|decir|avisar)(le)?\b.{0,30}claude|ahorita regreso|en cuanto (me devuelva|termine)|"
    r"le (voy a |)(digo|aviso|pido|encargo) a claude|"
    r"\b(le pido|le paso|ah[ií] va|ahi le va|voy[.!]|en eso estoy)\b.{0,80}|.{0,80}\bclaude\b.{0,120}(⏳|voy|ahorita|en cuanto|te (aviso|llega|mando))|"
    r"en un (momento|rato) te|voy a (buscar|revisar|checar|jalar|bajar)|ahorita le entro|le entro ahorita",
    re.I)


async def llamar_deepseek(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat: int) -> tuple[str, list[str], bool]:
    await refrescar_permisos()
    await asyncio.to_thread(agenda_contexto, jarvis.ahora())  # precarga sin bloquear el bot
    hist = history(chat)
    consulta = next((m["content"] for m in reversed(hist) if m["role"] == "user"), "")
    messages = [{"role": "system", "content": system_prompt(consulta)}] + hist + [nota_hoy()]
    opciones: list[str] = []
    con_voz = False
    herramientas_usadas = 0
    delegados = 0  # Claude tiene límite diario: máximo un trabajo por mensaje de Chava
    correcciones = 0
    previo: list[str] = []  # texto que DeepSeek escribe junto con una llamada a herramienta (p. ej. el plan antes de los botones)
    for _ in range(6):  # máx. 6 rondas de herramientas por mensaje
        resp = await deepseek_chat(
            "chat", model=DEEPSEEK_MODEL, messages=messages, tools=herramientas(), temperature=1.1)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            texto = msg.content or ""
            if herramientas_usadas == 0 and correcciones < 2 and AFIRMA_HECHO.search(texto):
                # Dice que ya hizo algo (agendar, borrar, enviar…) sin haber llamado ninguna herramienta: mentira.
                log.info("Afirmó algo hecho sin herramienta; se le obliga a hacerlo de verdad")
                correcciones += 1
                messages.append({"role": "assistant", "content": texto})
                messages.append({"role": "user", "content":
                    "[SISTEMA: Dijiste que YA hiciste algo, pero en este turno NO llamaste ninguna herramienta, así que "
                    "NO se hizo. Hazlo ahora de verdad con la herramienta correcta y responde según su resultado. "
                    "Si no se puede, dile honestamente por qué.]"})
                continue
            if herramientas_usadas == 0 and correcciones < 2 and PROMESA.search(texto) and not texto.rstrip(" *_🤙👇😅").endswith("?"):
                # Prometió hacer algo sin llamar ninguna herramienta: si no actúa ahora, nunca pasa.
                log.info("Promesa sin herramienta detectada; se le obliga a actuar")
                correcciones += 1
                messages.append({"role": "assistant", "content": texto})
                messages.append({"role": "user", "content":
                    "[SISTEMA: Acabas de prometer hacer algo SIN llamar ninguna herramienta, y tú no sigues trabajando "
                    "después de responder. Si ya puedes contestar con lo que sabes (la fecha y hora están en tu "
                    "contexto), contesta directo. Si hace falta una herramienta rápida (calendario, correo, Classroom, "
                    "archivos, notas), llámala. NO uses delegar_a_claude solo para cumplir la promesa: es solo para "
                    "crear/editar archivos o trabajo pesado. Si nada sirve, dile honestamente qué falta.]"})
                continue
            if previo and texto.strip() not in "\n".join(previo):
                texto = "\n\n".join(previo + [texto]) if len(texto) < 400 else texto
            return texto, opciones, con_voz
        if (msg.content or "").strip():
            previo.append(msg.content.strip())
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        herramientas_usadas += sum(tc.function.name != "habilidad" for tc in msg.tool_calls)  # abrir un manual no es actuar
        for tc in msg.tool_calls:
            estadisticas.registrar_herramienta(db, tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
                if tc.function.name == "delegar_a_claude":
                    if delegados:
                        log.info("Segunda delegación en el mismo mensaje bloqueada: %s", args.get("titulo"))
                        out = ("Bloqueado: ya mandaste un trabajo a Claude en este mensaje y su límite diario es escaso. "
                               "No lo vuelvas a intentar; contesta con lo que tienes.")
                    else:
                        delegados += 1
                        out = await tool_delegar(update, ctx, args)
                elif tc.function.name == "listar_archivos":
                    out = tool_listar(args.get("materia", ""), args.get("parcial", ""))
                elif tc.function.name == "impresora_estado":
                    out = json.dumps(await asyncio.to_thread(impresora.estado), ensure_ascii=False)
                elif tc.function.name == "imprimir":
                    out = await tool_imprimir(ctx, chat, args)
                elif tc.function.name == "mandar_archivo":
                    out = await tool_mandar_archivo(ctx, chat, args)
                elif tc.function.name == "contestar_con_voz":
                    con_voz = True
                    out = "Listo: tu respuesta le llegará como nota de voz. Ahora escribe lo que le vas a decir."
                elif tc.function.name == "ofrecer_opciones":
                    opciones = [str(o) for o in args.get("opciones", [])][:6]
                    out = "Listo, los botones saldrán debajo de tu mensaje. Ahora escribe la pregunta."
                elif tc.function.name == "recordatorio_crear":
                    out = await tool_recordatorio(ctx.application, args)
                elif tc.function.name == "sesion_claude":
                    out = await tool_sesion_claude(args)
                elif tc.function.name == "mi_dinero":
                    out = await tool_mi_dinero(args)
                elif tc.function.name == "mi_empleo":
                    out = tool_mi_empleo(args)
                elif tc.function.name == "gasto_registrar":
                    out = await tool_gasto(args)
                elif tc.function.name == "gasto_borrar_ultimo":
                    out = await tool_gasto(args, borrar=True)
                elif tc.function.name == "vacante_estado":
                    out = tool_vacante_estado(args)
                elif tc.function.name == "cv_a_la_medida":
                    texto = str(args.get("vacante", "")).strip()
                    if len(texto) < 15:
                        out = "Faltan datos: pídele el puesto y la empresa (o que pegue la vacante)."
                    elif not claude_activo():
                        out = "Claude está apagado: dile que lo prenda con /claude on."
                    else:
                        v = empleos.vacante_de_cv(await datos_vacante(texto), texto)
                        await iniciar_cv(ctx, chat, v["id"])
                        out = (f"Listo: ya le mandé el link y el CV de «{v['puesto']}» en {v['empresa']} le llega en 2-3 min. "
                               "En su app sale en Jobs → Listos para aplicar. No repitas lo que ya le mandé, solo confírmale corto.")
                else:
                    out = ejecutar_tool_jarvis(tc.function.name, args)
                    if out is None:
                        out = ejecutar_tool_archivos(tc.function.name, args)
                    if out is None:
                        out = await ejecutar_tool_google(ctx, chat, tc.function.name, args)
                    if out is None:
                        out = "Herramienta desconocida"
                    out = out or "(resultado vacío)"
            except Exception as e:
                log.exception("tool error")
                out = f"Error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return "No mames, me hice bolas con las herramientas 😅 ¿me lo repites de otra forma?", [], False


# ---------- seguimiento de trabajos ----------

def ruta_de_job(rel: str) -> Path:
    """Rutas que devuelve el worker: 'JARVIS/...' = espacio personal, 'Creaciones/...'; lo demás = escuela."""
    if rel.startswith("JARVIS/"):
        return jarvis.BASE / rel[len("JARVIS/"):]
    if rel.startswith("Creaciones/"):
        return CREACIONES / rel[len("Creaciones/"):]
    return WORKSPACE / rel


class _ChatFalso:
    """Lo mínimo de telegram.Chat que usan responder()/llamar_deepseek() cuando KORRA habla por iniciativa propia."""
    def __init__(self, bot, chat_id: int):
        self.id, self._bot = chat_id, bot

    async def send_action(self, accion):
        await self._bot.send_chat_action(self.id, accion)

    async def send_message(self, *a, **k):
        return await self._bot.send_message(self.id, *a, **k)


async def continuar_tras_job(ctx: ContextTypes.DEFAULT_TYPE, chat: int, titulo: str):
    """Cuando Claude termina, KORRA revisa si había quedado de hacer algo con el resultado (p. ej. cargar un
    horario al calendario) y lo hace. Si no, se queda callada."""
    from types import SimpleNamespace
    upd = SimpleNamespace(effective_chat=_ChatFalso(ctx.bot, chat))
    add_msg(chat, "user", f"[SISTEMA: Claude terminó «{titulo}» (su resultado está arriba). Si le prometiste a Chava hacer "
                          "algo con ese resultado (agendar, cargar el horario, mandar un correo, recordatorios…), HAZLO "
                          "AHORA con las herramientas y dile qué hiciste. Si no quedaste de hacer nada, responde "
                          "exactamente: NADA]")
    try:
        texto, opciones, _ = await llamar_deepseek(upd, ctx, chat)
    except Exception:
        log.exception("continuación tras job")
        return
    if not texto or texto.strip().upper().startswith("NADA"):
        db.execute("DELETE FROM msgs WHERE id=(SELECT max(id) FROM msgs WHERE chat=? AND role='user')", (chat,))
        db.commit()
        return
    add_msg(chat, "assistant", texto)
    await enviar(ctx.bot, chat, texto, teclado_opciones(ctx, opciones) if opciones else None)


async def esperar_job(ctx: ContextTypes.DEFAULT_TYPE, chat: int, job_id: str, titulo: str, aviso_id: int | None = None):
    bot = ctx.bot
    while True:
        await asyncio.sleep(10)
        try:
            r = await worker.get(f"/jobs/{job_id}")
        except Exception:
            continue  # worker reiniciándose: se reintenta
        if r.status_code == 404:
            # El worker ya no lo tiene (lo olvidó en una limpieza). Ya no se relanza solo: cuesta límite.
            guardar_job_info(job_id, pendiente=False)
            if aviso_id:
                try:
                    await bot.edit_message_reply_markup(chat, aviso_id, reply_markup=None)
                except Exception:
                    pass
            await bot.send_message(chat, f"❌ Se perdió el trabajo «{titulo}» en el servidor.",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                       "🔁 Rehacer", callback_data=f"job:redo:{job_id}")]]))
            add_msg(chat, "assistant", f"[El trabajo de Claude «{titulo}» se perdió; NO se hizo.]")
            return
        job = r.json()
        if job["status"] in ("queued", "running"):
            continue
        break

    if aviso_id:  # quita el botón de Cancelar del aviso ⏳
        try:
            await bot.edit_message_reply_markup(chat, aviso_id, reply_markup=None)
        except Exception:
            pass
    guardar_job_info(job_id, pendiente=False)
    icon = {"done": "✅", "cancelled": "🛑", "timeout": "⌛"}.get(job["status"], "❌")
    mins = (job.get("finished", 0) - job.get("started", job.get("finished", 0))) / 60
    resultado = rutas_visibles(job.get("result") or "")
    costo = f" · ${job['cost_usd']:.2f}" if job.get("cost_usd") else ""
    texto = f"{icon} «{titulo}» — {ESTADOS_JOB.get(job['status'], job['status'])} ({mins:.1f} min{costo})\n\n{resultado}"
    if revision := texto_revision(job):
        texto += "\n\n" + revision
    borrados = [d for d in job.get("deleted", []) if not d.startswith(ENTRADA_DIR) and "/node_modules/" not in d]
    if borrados:
        texto += "\n\n🗑 Borrados: " + ", ".join(borrados[:10])
    if aviso := AVISO_ERROR.get(job.get("error_tipo", "")):
        texto += "\n\n" + aviso
    partes = [texto[i:i + 4000] for i in range(0, len(texto), 4000)]
    for i, parte in enumerate(partes):
        ultimo = i == len(partes) - 1 and job["status"] != "cancelled"
        await bot.send_message(chat, parte, reply_markup=teclado_job_terminado(job_id, job) if ultimo else None)
    add_msg(chat, "assistant", f"[Resultado de Claude para «{titulo}» ({job['status']})]: {resultado}")
    if job.get("session_id"):
        kv_set("ultima_sesion", job["session_id"])  # para /continuar y `continuar_sesion`
    if job["status"] == "done":
        ctx.application.create_task(continuar_tras_job(ctx, chat, titulo))

    if jobs_info().get(job_id, {}).get("enviar", True):
        await enviar_archivos_job(ctx, chat, job)
    elif n := len(archivos_de_job(job)):
        # Espacio personal: no se mandan solos (nóminas, documentos), pero quedan a un toque.
        await bot.send_message(chat, f"📎 Claude dejó {n} archivo(s) en tus carpetas (se ven en Samba).",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                   f"📲 Mándamelos ({n})", callback_data=f"job:files:{job_id}")]]))


def nombre_visible(rel: str) -> str:
    """Ruta del worker → como se ve en Samba."""
    return a_samba(rel if rel.startswith(("JARVIS/", "Creaciones/")) else f"CETI COLOMOS/{rel}")


def archivos_de_job(job: dict) -> list[str]:
    """Lo que Claude declaró como entrega (el worker ya lo filtró); en trabajos viejos, lo cambiado sin código."""
    if "entregables" in job:
        return [rel for rel in job["entregables"] if ruta_de_job(rel).is_file()]
    return [rel for rel in job.get("changed", [])
            if ruta_de_job(rel).is_file() and not rel.startswith(ENTRADA_DIR)
            and ruta_de_job(rel).suffix not in (".py", ".pyc", ".log")]


ESTADOS_JOB = {"done": "listo", "error": "falló", "timeout": "se acabó el tiempo", "cancelled": "cancelado"}
AVISO_ERROR = {
    "limite": "🔋 Se acabó tu límite de Claude por ahora (mira /uso). Lo puedo reintentar más tarde.",
    "timeout": "⌛ No le alcanzó el tiempo. Puedo seguir la misma sesión para que termine lo que falta.",
    "interrumpido": "🔌 Se reinició el servidor a medio trabajo. Puedo seguir la sesión o rehacerlo.",
}


def texto_revision(job: dict) -> str:
    """Lo que encontró la revisión automática del worker: una línea por archivo entregado."""
    lineas = []
    for rel, r in (job.get("revision") or {}).items():
        nombre = Path(rel).name
        if r.get("ok"):
            lineas.append(f"✅ {nombre}" + (f" · {' · '.join(r['detalles'])}" if r.get("detalles") else ""))
        else:
            lineas.append(f"⚠️ {nombre}: {', '.join(r['problemas'])}")
    if lineas:
        donde = {nombre_visible(str(Path(rel).parent)) for rel in job.get("entregables", [])}
        lineas.append("📁 " + ", ".join(sorted(donde)))
    return "\n".join(lineas)


async def mandar_video_comprimido(bot, chat: int, rel: str) -> bool:
    """Video de más de 50 MB: el worker hace una copia que cabe en Telegram (el original se queda intacto)."""
    await bot.send_message(chat, f"🎬 {Path(rel).name} pesa mucho para Telegram; te mando una copia comprimida…")
    try:
        r = await worker.post("/comprimir", json={"ruta": rel}, timeout=1900)
        r.raise_for_status()
        tmp = Path("/tmp") / r.json()["tmp"]
        async with worker.stream("GET", f"/archivo/{tmp.name}", timeout=300) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                async for trozo in resp.aiter_bytes():
                    fh.write(trozo)
        try:
            await mandar_archivo_tg(bot, chat, tmp, f"{nombre_visible(rel)} (copia comprimida; el original está en Samba)")
        finally:
            tmp.unlink(missing_ok=True)
        return True
    except Exception:
        log.exception("comprimir %s", rel)
        return False


async def enviar_archivos_job(ctx: ContextTypes.DEFAULT_TYPE, chat: int, job: dict):
    bot = ctx.bot
    enviados = 0
    for rel in archivos_de_job(job):
        p = ruta_de_job(rel)
        if enviados >= MAX_FILES:
            await bot.send_message(chat, f"(hay más archivos; revisa la carpeta en Samba)")
            break
        try:
            if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                if p.suffix.lower() not in VIDEOS or not await mandar_video_comprimido(bot, chat, rel):
                    await bot.send_message(chat, f"📦 {nombre_visible(rel)} pesa más de {MAX_FILE_MB} MB; ábrelo desde Samba.")
                continue
            await mandar_archivo_tg(bot, chat, p, nombre_visible(rel), boton_imprimir(ctx, p))
            enviados += 1
        except Exception as e:  # que un archivo que no sube no impida mandar los demás
            log.exception("enviar %s", rel)
            await bot.send_message(chat, f"⚠️ No pude mandar {nombre_visible(rel)} ({e}); está en tus carpetas (Samba).")


# ---------- iniciativa propia: mensajes programados ----------

async def mensaje_proactivo(app: Application, instruccion: str, reply_markup=None, forzar: bool = False,
                            respaldo: str = ""):
    """Le pide a DeepSeek un mensaje por iniciativa propia y se lo manda a Chava.
    `respaldo`: texto que se manda tal cual si DeepSeek falla o no escribe nada (p. ej. un recordatorio)."""
    await refrescar_permisos()
    await asyncio.to_thread(agenda_contexto, jarvis.ahora())  # precarga sin bloquear el bot
    messages = [{"role": "system", "content": system_prompt()}] + history(ALLOWED_CHAT_ID) + [
        {"role": "user", "content": f"[SISTEMA — Chava no escribió esto. {instruccion} "
                                    "Escribe SOLO el mensaje que le vas a mandar. Si de verdad no tiene "
                                    "sentido escribirle ahora, responde exactamente: NADA]"}, nota_hoy()]
    try:
        resp = await deepseek_chat("proactivo", model=DEEPSEEK_MODEL, messages=messages, temperature=1.2)
        texto = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("mensaje proactivo")
        texto = ""
    es_nada = texto.strip(" .!¡\"'*").upper() == "NADA"
    if not texto or (es_nada and forzar):
        texto = respaldo
    elif es_nada:
        return
    if not texto:
        return
    add_msg(ALLOWED_CHAT_ID, "assistant", texto)
    await enviar(app.bot, ALLOWED_CHAT_ID, texto, reply_markup)


async def agenda_texto(dia: datetime, con_correos: bool = False) -> str:
    """Resumen de agenda + tareas + correos para los mensajes de la mañana/noche."""
    if not g.configurado() or not g.conectadas():
        return ""
    partes = []
    fecha = dia.strftime("%Y-%m-%d")
    try:
        ev = await asyncio.to_thread(g.cal_listar, fecha, fecha)
        partes.append("Agenda de ese día:\n" + ("\n".join(
            f"- {'todo el día' if e['todo_el_dia'] else e['inicio'][11:]}: {e['titulo']}" + (f" ({e['lugar']})" if e['lugar'] else "")
            for e in ev) or "(nada)"))
    except Exception as e:
        log.warning("agenda: %s", e)
    limite = (dia + timedelta(days=7)).isoformat()
    prox = [t for t in tareas_pendientes() if t["entrega"] and t["entrega"] <= limite]
    evs = eventos_proximos(7)
    if evs:
        partes.append("Eventos de software en GDL esta semana:\n" + "\n".join(
            f"- {e['nombre']} — {e['inicio']} ({e.get('estado')})" for e in evs))
    partes.append("Tareas que se entregan en los próximos 7 días:\n" + ("\n".join(
        f"- {t['materia'] or t['curso']}: {t['titulo']} — {t['entrega'][:16].replace('T', ' ')}" for t in prox) or "(ninguna)"))
    if con_correos:
        try:
            cs = await asyncio.to_thread(g.gmail_buscar, "is:unread newer_than:1d category:primary", "", 8)
            partes.append("Correos sin leer importantes:\n" + ("\n".join(
                f"- ({c['cuenta']}) {c['de']}: {c['asunto']}" for c in cs) or "(ninguno)"))
        except Exception as e:
            log.warning("correos: %s", e)
    return "\n\n".join(partes)


FINANZAS_URL = os.environ.get("FINANZAS_URL", "http://task-hub:8000/api/v1")


MIS_TOOLS = [
    _f("mi_dinero", ("Sus finanzas REALES ahora mismo (solo lectura, de su sistema task-hub, lo mismo que ve en la app): "
                     "efectivo de la semana, fondo de tarjetas, ahorro, tarjetas (a pagar, fecha límite, ciclo, compras a "
                     "meses), alertas y gastos recientes. Úsala SIEMPRE antes de opinar de su dinero o de si le alcanza."),
       {"detalle": {"type": "string", "enum": ["resumen", "gastos", "meses"],
                    "description": "resumen (default) · gastos = últimos movimientos y en qué se va · meses = compras a meses por tarjeta"}}),
    _f("gasto_registrar", ("Registra un gasto en su sistema de dinero (lo mismo que su atajo \"Gasté\"). Úsala cuando diga «gasté…», «me "
                           "compré…», «pagué…». Si no dice con qué pagó, pregúntale (efectivo o qué tarjeta). Te regresa el mensaje "
                           "del sistema (cuánto le queda): díselo tal cual."),
       {"monto": {"type": "number", "description": "pesos, positivo"},
        "metodo": {"type": "string", "description": "'efectivo' o el id de una tarjeta (nu, plata… los ves en mi_dinero)"},
        "categoria": {"type": "string", "enum": ["Comida", "Transporte", "Escuela", "Salidas", "Personal", "Otros"]},
        "nota": {"type": "string", "description": "qué fue, corto (ej. 'tacos', 'Uber a la escuela')"},
        "meses": {"type": "integer", "description": "0 = contado; 3, 6, 9, 12… = meses sin intereses (solo tarjeta)"}},
       ["monto", "metodo", "categoria"]),
    _f("gasto_borrar_ultimo", "Borra el ÚLTIMO gasto registrado (solo de las últimas 24 h). Para \"me equivoqué\". Confírmale cuál era.", {}),
    _f("cv_a_la_medida", ("Arma su CV a la medida para una vacante que te pegó o describió (lo mismo que /cv + detalles): "
                          "la registra en su app (Jobs → Listos para aplicar, con «¿Ya aplicaste?»), le manda el link y el "
                          "CV en PDF llega solo en 2-3 min. Úsala SIEMPRE para CVs a la medida, no delegues a Claude a mano. "
                          "Para una vacante que ya está en su lista, igual pásale el puesto y la empresa."),
       {"vacante": {"type": "string", "description": "TODO lo que dijo/pegó de la vacante: puesto, empresa, requisitos, link…"}},
       ["vacante"]),
    _f("vacante_estado", ("Cambia el estado de una vacante de su búsqueda de empleo: cuando diga que ya aplicó, que le interesa o "
                          "que no le interesa. Busca por empresa y/o puesto. Si hay varias, te las regresa para que le preguntes cuál."),
       {"buscar": {"type": "string", "description": "empresa y/o puesto, ej. 'Continental soporte'"},
        "estado": {"type": "string", "enum": ["me_interesa", "aplicada", "descartada", "nueva"]}},
       ["buscar", "estado"]),
    _f("mi_empleo", ("Su búsqueda de empleo (solo lectura): vacantes que se le encontraron y su estado, a cuáles aplicó, "
                     "correos a reclutadores (enviados, quién contestó), sus CVs y su perfil de la entrevista."),
       {"que": {"type": "string", "enum": ["resumen", "vacantes", "aplicadas", "correos", "cvs", "perfil"],
                "description": "resumen (default) o una parte"}}),
]


async def _finanzas(ruta: str) -> dict | list:
    token = os.environ.get("FINANZAS_TOKEN", "")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{FINANZAS_URL}/{ruta}", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def tool_mi_dinero(args: dict) -> str:
    detalle = args.get("detalle") or "resumen"
    d = await _finanzas("resumen")
    tarjetas = [{k: t.get(k) for k in ("nombre", "a_pagar", "fecha_limite", "dias_para_pagar", "ciclo_actual", "deuda",
                                       "proximo_corte", "tope_personal", "meses_restante")} for t in d.get("tarjetas", [])]
    if detalle == "meses":
        return _j({"tarjetas": [{"nombre": t["nombre"], "meses": [{k: m.get(k) for k in ("descripcion", "mensualidad", "restante", "pagos")}
                                                                    for m in t.get("meses", [])]} for t in d.get("tarjetas", [])]})
    if detalle == "gastos":
        movs = await _finanzas("movimientos?limit=40")
        return _j({"categorias_semana": d.get("categorias_semana"), "categorias_mes": d.get("categorias_mes"),
                   "movimientos": [{"fecha": datetime.fromtimestamp(m["ts"], jarvis.TZ).strftime("%Y-%m-%d %H:%M"),
                                    **{k: m.get(k) for k in ("tipo", "monto", "metodo", "categoria", "nota")}} for m in movs]})
    sem = d.get("semana") or {}
    return _j({"hoy": d.get("hoy"), "efectivo_para_gastar": d.get("efectivo"), "semana": {k: sem.get(k) for k in (
               "limite", "gastado", "por_dia", "proximo_pago", "dias_para_pago")},
               "fondo_tarjetas": d.get("fondo"), "debe_del_corte": d.get("a_pagar_total"), "deuda_total": d.get("deuda_total"),
               "ahorro_total": d.get("ahorro_total"), "cajas": [{k: c.get(k) for k in ("nombre", "saldo", "rendimiento_anual")} for c in d.get("cajas", [])],
               "patrimonio": d.get("patrimonio"), "tarjetas": tarjetas, "alertas": [a["texto"] for a in d.get("alertas", [])],
               "reparto": f"{'/'.join(str(x) for x in d.get('proporcion', []))} (gastos/fondo/ahorro)",
               "reglas": "El fondo solo paga tarjetas; el ahorro nunca paga tarjetas; se paga el corte completo antes de la fecha límite."})


async def tool_gasto(args: dict, borrar: bool = False) -> str:
    token = os.environ.get("FINANZAS_TOKEN", "")
    async with httpx.AsyncClient(timeout=15) as c:
        if borrar:
            r = await c.delete(f"{FINANZAS_URL}/gastos/ultimo", headers={"Authorization": f"Bearer {token}"})
        else:
            cuerpo = {k: args[k] for k in ("monto", "metodo", "categoria", "nota", "meses") if args.get(k) not in (None, "")}
            r = await c.post(f"{FINANZAS_URL}/gastos", json=cuerpo, headers={"Authorization": f"Bearer {token}"})
    datos = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        return f"No se pudo: {datos.get('error') or datos.get('detail') or r.status_code}"
    return datos.get("mensaje") or _j(datos)


def tool_vacante_estado(args: dict) -> str:
    vs = empleos.buscar_vacantes(args.get("buscar", ""))
    if not vs:
        return "No encontré esa vacante en su lista (mi_empleo para verlas)."
    if len(vs) > 1:
        return "Hay varias, pregúntale cuál: " + _j([{"puesto": v["puesto"], "empresa": v["empresa"], "estado": v.get("estado")} for v in vs[:8]])
    v = empleos.poner_estado(vs[0]["id"], args["estado"])
    return f"Listo: «{v['puesto']}» en {v['empresa']} quedó como {args['estado']} (se ve en su pestaña Jobs)."


def tool_mi_empleo(args: dict) -> str:
    que = args.get("que") or "resumen"
    vs = empleos.cargar()
    corto = lambda v: {k: v.get(k) for k in ("puesto", "empresa", "perfil", "zona", "modalidad", "ingles", "sueldo", "match", "estado", "link")}
    cs = ce.contactos()
    if que == "vacantes":
        return _j([corto(v) for v in empleos.activas()[:15]])
    if que == "aplicadas":
        return _j([corto(v) for v in vs if v.get("estado") == "aplicada"])
    if que == "correos":
        return _j({"resumen": ce.resumen(), "contestaron": [{k: c.get(k) for k in ("empresa", "correo", "estado", "respondio")}
                                                             for c in cs if c.get("estado") in ("respondio", "no_contactar")],
                   "ultimos_envios": ce.envios()[-10:]})
    if que == "cvs":
        return _j([f.name for f in empleos.lista_cvs(15)])
    if que == "perfil":
        try:
            return (empleos.DIR / "Perfil" / "perfil.md").read_text(encoding="utf-8")[:6000]
        except OSError:
            return "Todavía no hay perfil: que haga la entrevista en su página Empleo."
    return _j({"vacantes": {e: sum(1 for v in vs if v.get("estado") == e) for e in empleos.ESTADOS},
               "mejores_activas": [corto(v) for v in empleos.activas()[:5]],
               "aplicadas": [f"{v['puesto']} — {v['empresa']}" for v in vs if v.get("estado") == "aplicada"],
               "correos": ce.resumen(), "cvs_generales": [f.name for f in empleos.lista_cvs(2)],
               "gmail_empleo_conectado": empleo_conectado()})


async def dinero_texto() -> str:
    """Lo esencial de la API de dinero (task-hub) para el resumen de la mañana. '' si no está configurada o no responde."""
    token = os.environ.get("FINANZAS_TOKEN", "")
    if not token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{FINANZAS_URL}/resumen", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning("dinero: %s", e)
        return ""
    sem = d.get("semana") or {}
    lineas = [f"Efectivo para gastos: ${d.get('efectivo', 0):,.2f}; esta semana lleva ${sem.get('gastado', 0):,.2f} de "
              f"${sem.get('limite', 0):,.2f} y faltan {sem.get('dias_para_pago', '?')} días para su pago."]
    for t in d.get("tarjetas", []):
        if t.get("a_pagar"):
            lineas.append(f"Tarjeta {t['nombre']}: pagar ${t['a_pagar']:,.2f} antes del {t.get('fecha_limite')} "
                          f"(faltan {t.get('dias_para_pagar')} días).")
    lineas += [f"Aviso: {a['texto']}" for a in d.get("alertas", [])[:3]]
    return "\n".join(lineas)


async def job_manana(ctx: ContextTypes.DEFAULT_TYPE):
    if nivel_iniciativa() == "off" or float(kv_get("silencio_hasta", "0") or 0) > time.time():
        return
    agenda = await agenda_texto(jarvis.ahora(), con_correos=True)
    datos = [agenda, "Servidor y respaldos:\n" + estado_servidor.resumen_para_prompt()]
    if dinero := await dinero_texto():
        datos.append("Dinero:\n" + dinero)
    if semana := resumen_semana():
        datos.append("Tu resumen de la semana (es domingo, cuéntaselo):\n" + semana)
    await mensaje_proactivo(ctx.application,
        "Es su mañana. Mándale el primer mensaje del día como su compa: salúdalo a tu manera "
        "(varía, no siempre igual) y ármale su itinerario de hoy en pocas líneas: horarios en orden, "
        "tareas que se le vienen y correos que valgan la pena. Si el día está pesado o hay choques, "
        "díselo y proponle cómo acomodarse. Del dinero menciona solo lo que importe hoy (un pago que se acerca, "
        "si va pasado de gasto). Del servidor y los respaldos, solo una línea corta si todo bien, o el problema "
        "si hay uno. Si es domingo, incluye el resumen de la semana. Si no hay nada, algo breve de lo que platicaron ayer."
        + "\n\nDATOS:\n" + "\n\n".join(x for x in datos if x))


async def job_noche(ctx: ContextTypes.DEFAULT_TYPE):
    if nivel_iniciativa() == "off" or float(kv_get("silencio_hasta", "0") or 0) > time.time():
        return
    manana = await agenda_texto(jarvis.ahora() + timedelta(days=1))
    await mensaje_proactivo(ctx.application,
        (f"(Datos de MAÑANA para que se lo anticipes:\n{manana})\n" if manana else "") +
        "Es de noche. Hazle un check-in: pregúntale cómo le fue, dale seguimiento a algo concreto "
        "del día o de tu memoria, y si mañana tiene algo, recuérdaselo. Si hoy ya platicaron "
        "bastante y no hay nada nuevo que decir, puedes responder NADA. Corto.")


def _ventana_dia(fecha: str) -> tuple[float, float]:
    """El "día" de una entrada de diario va de las 4:00 de esa fecha a las 4:00 del siguiente (la reflexión corre de madrugada)."""
    ini = datetime.strptime(fecha, "%Y-%m-%d").replace(hour=4, tzinfo=jarvis.TZ)
    return ini.timestamp(), (ini + timedelta(days=1)).timestamp()


async def reflexionar_dia(fecha: str) -> dict | None:
    """Relee la plática de `fecha`, escribe su diario y aplica a la memoria lo que aprendió (patrón mem0: por cada
    dato decide nuevo / actualizar / olvidar, y fusiona duplicados). Devuelve el conteo, o None si no hubo plática."""
    desde, hasta = _ventana_dia(fecha)
    rows = db.execute("SELECT role, content FROM msgs WHERE chat=? AND ts>=? AND ts<? ORDER BY id",
                      (ALLOWED_CHAT_ID, desde, hasta)).fetchall()
    if not rows:
        return None
    charla = "\n".join(f"{'Chava' if r == 'user' else NOMBRE}: {c}" for r, c in rows)[-30000:]
    memoria.caducar()
    activos = memoria.buscar("", n=400)
    lista = "\n".join(f"#{r['id']} [{r['categoria']}, imp {r['importancia']}"
                      + (f", hasta {r['vigente_hasta']}" if r["vigente_hasta"] else "") + f"] {r['texto']}" for r in activos)
    prompt = f"""{jarvis.personalidad()}

Es de madrugada y estás a solas reflexionando sobre el día {fecha} con Chava.
Su perfil:
<perfil>
{jarvis.perfil() or '(sin perfil)'}
</perfil>
Tu memoria actual (un dato por línea, con su #id):
<memoria>
{lista}
</memoria>

Esta fue la plática de ese día:
<platica>
{charla}
</platica>

Revisa cada cosa que aprendiste en la plática contra tu memoria y decide: es NUEVA, ACTUALIZA un dato que ya
tienes (dale su id), deja SIN VALIDEZ uno viejo, o ya la sabías (no hagas nada). Devuelve un JSON con:
- "diario": tu entrada de diario de ese día, en primera persona y con tu personalidad (5-12 líneas): qué pasó,
  cómo viste a Chava, qué aprendiste de él, qué quieres darle seguimiento.
- "nuevos": datos NUEVOS que no estén ya en la memoria:
  [{{"texto", "categoria" ({", ".join(memoria.CATEGORIAS)}), "importancia", "vigente_hasta" "AAAA-MM-DD" o ""}}]
  Importancia: 3 = esencial para hablarle bien (pocos), 2 = útil a futuro, 1 = de un día o anécdota (con vigente_hasta).
- "actualizar": datos existentes que cambiaron: [{{"id", "texto", "categoria", "importancia", "vigente_hasta"}}]
- "olvidar": datos que ya no aplican (pendiente cumplido, algo que resultó falso, ya pasó): [{{"id", "motivo"}}]
- "fusionar": grupos de datos que dicen LO MISMO (duplicados): [{{"quedar": id, "texto": "versión unida y completa",
  "quitar": [ids]}}]. Revisa toda la memoria, no solo lo de hoy.
- "seguimientos": cosas que conviene PREGUNTARLE después porque van a pasar o quedaron abiertas (un examen,
  una entrevista, un trámite, cómo se sentía): [{{"texto", "cuando" "AAAA-MM-DD HH:MM"}}] (cuando = un poco
  después de que pase; no repitas los que ya están en la memoria como seguimiento).
- "nucleo": ids que deberían ser importancia 3 (lo esencial para hablarle bien mañana; máx. ~25) y
  "quitar_nucleo": ids que ya no son esenciales (pasan a 2).
Reglas: no inventes nada que no esté en la plática o en la memoria; fechas absolutas; no olvides datos
permanentes solo porque no se hablaron hoy. Listas vacías si no hay cambios."""
    resp = await deepseek_chat(
        "reflexion", model=DEEPSEEK_MODEL, messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, temperature=0.5)
    data = json.loads(resp.choices[0].message.content)
    if data.get("diario"):
        jarvis.diario_escribir(fecha, data["diario"])
        memoria.indexar_diario(fecha, data["diario"])
    n = {"nuevos": 0, "actualizados": 0, "olvidados": 0, "fusionados": 0, "seguimientos": 0}
    for x in data.get("nuevos", []):
        if x.get("texto"):
            memoria.recordar(x["texto"], x.get("categoria", "otro"), x.get("importancia", 2),
                             x.get("vigente_hasta") or None, fuente="reflexion")
            n["nuevos"] += 1
    for x in data.get("actualizar", []):
        if x.get("id") and x.get("texto"):
            memoria.recordar(x["texto"], x.get("categoria", "otro"), x.get("importancia", 2),
                             x.get("vigente_hasta") or None, id=int(x["id"]), fuente="reflexion")
            n["actualizados"] += 1
    for x in data.get("seguimientos", []):
        try:
            if x.get("texto") and x.get("cuando"):
                programar_seguimiento(x["texto"], x["cuando"])
                n["seguimientos"] += 1
        except ValueError:
            pass
    vivos = {r["id"]: r for r in activos}
    olvidos = [(int(x["id"]), x.get("motivo", "reflexión nocturna")) for x in data.get("olvidar", []) if x.get("id")]
    for x in data.get("fusionar", []):
        quedar = int(x.get("quedar") or 0)
        if quedar in vivos and x.get("texto"):
            r = vivos[quedar]
            memoria.recordar(x["texto"], r["categoria"], r["importancia"], r["vigente_hasta"], id=quedar, fuente="reflexion")
            olvidos += [(int(q), f"duplicado de #{quedar}") for q in x.get("quitar", []) if int(q) != quedar]
            n["fusionados"] += 1
    if len(olvidos) > max(10, len(activos) // 5):  # freno: nunca olvidar en masa en una sola noche
        log.warning("Reflexión quiso olvidar %d datos; se ignoran por seguridad", len(olvidos))
        olvidos = []
    for rid, motivo in olvidos:
        if rid in vivos:
            memoria.olvidar(rid, motivo)
            n["olvidados"] += 1
    for rid, imp in [(i, 3) for i in data.get("nucleo", [])] + [(i, 2) for i in data.get("quitar_nucleo", [])]:
        memoria.db.execute("UPDATE recuerdos SET importancia=? WHERE id=? AND estado='activo'", (imp, int(rid)))
    memoria.db.commit()
    memoria.exportar_md()
    return n


async def job_reflexion(ctx: ContextTypes.DEFAULT_TYPE):
    """En la madrugada: reflexiona sobre ayer y, si una noche falló (DeepSeek vacío, la Pi apagada), también sobre
    los días anteriores que se quedaron sin diario."""
    hoy = jarvis.ahora() - timedelta(hours=6)  # a las 3:30 "el día" que acaba de pasar es el de ayer
    for atras in (2, 1, 0):
        fecha = (hoy - timedelta(days=atras)).strftime("%Y-%m-%d")
        if atras and (jarvis.DIARIO / f"{fecha}.md").exists():
            continue
        try:
            n = await reflexionar_dia(fecha)
            if n is not None:
                log.info("Reflexión (%s): %s", fecha, n)
        except Exception:
            log.exception("reflexión del %s", fecha)


SEMANAS = jarvis.DIARIO / "semanas"


async def job_reflexion_semanal(ctx: ContextTypes.DEFAULT_TYPE):
    """Domingo de madrugada: relee los diarios de la semana, saca patrones (patrón "reflexión" de Generative Agents),
    reescribe perfil.md y deja un resumen de la semana para el mensaje de la mañana."""
    if jarvis.ahora().weekday() != 6:
        return
    diarios = sorted(jarvis.DIARIO.glob("*.md"))[-7:]
    if not diarios:
        return
    semana = "\n\n".join(f"### {p.stem}\n{p.read_text(encoding='utf-8')}" for p in diarios)
    prompt = f"""{jarvis.personalidad()}

Es domingo de madrugada. Relees tu diario de la semana para entender mejor a Chava.
Tu perfil actual de él:
<perfil>
{jarvis.perfil() or '(vacío)'}
</perfil>
Tu núcleo de memoria:
<nucleo>
{memoria.nucleo()}
</nucleo>
Tu diario de la semana:
<diario>
{semana}
</diario>

Devuelve un JSON con:
- "perfil": su perfil reescrito en Markdown, MÁXIMO {jarvis.MAX_PERFIL - 200} caracteres. Quién es, su rutina,
  escuela y trabajo, cómo le gusta que le hables y trabajes, qué le importa ahora y cómo ha estado. Solo lo
  duradero (nada de pendientes de esta semana); conserva lo que sigue siendo cierto y corrige lo que cambió.
- "semana": resumen de la semana para mandárselo el domingo en la mañana, en tu voz (4-8 líneas): qué logró, qué
  quedó pendiente, un patrón que notaste y algo para la semana que viene.
- "aprendizajes": conclusiones generales que sacaste (patrones, no hechos sueltos), para tu memoria:
  [{{"texto", "categoria" ({", ".join(memoria.CATEGORIAS)}), "importancia" 2 o 3}}]
No inventes nada que no esté en el diario, el perfil o la memoria."""
    try:
        resp = await deepseek_chat(
            "reflexion_semanal", model=DEEPSEEK_MODEL, messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.5)
        data = json.loads(resp.choices[0].message.content)
        if len(data.get("perfil", "")) > 300:  # nunca reemplazar el perfil por algo casi vacío
            jarvis.perfil_escribir(data["perfil"])
        if data.get("semana"):
            SEMANAS.mkdir(parents=True, exist_ok=True)
            (SEMANAS / f"{jarvis.ahora():%G-W%V}.md").write_text(data["semana"].strip() + "\n", encoding="utf-8")
        for x in data.get("aprendizajes", [])[:8]:
            if x.get("texto"):
                memoria.recordar(x["texto"], x.get("categoria", "otro"), x.get("importancia", 2), fuente="reflexion_semanal")
        log.info("Reflexión semanal: perfil %d car., %d aprendizajes", len(data.get("perfil", "")), len(data.get("aprendizajes", [])))
    except Exception:
        log.exception("reflexión semanal")


def resumen_semana() -> str:
    """El resumen que dejó la reflexión semanal de hoy (solo los domingos)."""
    p = SEMANAS / f"{jarvis.ahora():%G-W%V}.md"
    return p.read_text(encoding="utf-8") if jarvis.ahora().weekday() == 6 and p.exists() else ""


async def job_recordatorio(ctx: ContextTypes.DEFAULT_TYPE):
    item = ctx.job.data
    if not any(r["id"] == item["id"] for r in jarvis.recordatorios()):
        return  # lo cancelaron
    jarvis.recordatorio_borrar(item["id"])
    await mensaje_proactivo(ctx.application,
        f"Llegó la hora de un recordatorio que programaste: «{item['texto']}». "
        "Recuérdaselo a tu manera, claro y directo. No respondas NADA.",
        forzar=True, respaldo=f"⏰ Recordatorio: {item['texto']}")


# ---------- Classroom: tareas nuevas + insistir hasta que las entregue ----------

async def marcar_tarea_hecha(tid: str):
    db.execute("UPDATE tareas SET estado='hecha' WHERE id=?", (tid,))
    db.commit()
    row = db.execute("SELECT evento_id FROM tareas WHERE id=?", (tid,)).fetchone()
    if row and row[0]:
        try:
            await asyncio.to_thread(g.evento_tarea_hecha, row[0])
        except Exception as e:
            log.warning("evento verde %s: %s", tid, e)


def teclado_tarea(tid: str, vencida: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya la entregué", callback_data=f"tarea:hecha:{tid}"),
        InlineKeyboardButton("🤖 Hazla con Claude", callback_data=f"tarea:claude:{tid}"),
    ]] + ([] if vencida else [[InlineKeyboardButton("⏰ En un rato", callback_data=f"tarea:luego:{tid}")]]))


SINONIMOS_CURSO = {"english": "ingles", "economics": "economia", "algebra": "algebra", "math": "matematicas"}
PALABRAS_VACIAS = {"a", "la", "el", "de", "del", "y", "en", "los", "las"}


def mapear_materia(curso: str) -> str | None:
    if m := buscar_materia(curso):
        return m
    n = norm(curso)
    for en, es in SINONIMOS_CURSO.items():
        n = re.sub(rf"\b{en}\b", es, n)
    if m := buscar_materia(n):
        return m
    # siglas: "P. O. E. 1Me" → PROGRAMACIÓN ORIENTADA A EVENTOS
    letras = re.sub(r"[^a-z]", "", n)
    for mat in materias():
        sigla = "".join(w[0] for w in norm(mat).split() if w not in PALABRAS_VACIAS)
        if len(sigla) >= 2 and letras.startswith(sigla):
            return mat
    curso = n
    palabras = lambda t: {w for w in re.findall(r"\w+", norm(t)) if len(w) > 3}
    tok = palabras(curso)
    mejor = max(materias(), key=lambda m: len(tok & palabras(m)), default=None)
    return mejor if mejor and tok & palabras(mejor) else None


def _nombre_carpeta(t: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", t).strip()[:80] or "tarea"


async def registrar_tarea(app: Application, t: dict, anunciar: bool):
    materia = mapear_materia(t["curso"])
    if materia:
        # El tema que puso el profe ("Parcial 2", "Segundo parcial"…) manda; si no hay, el parcial actual.
        tema = (await asyncio.to_thread(g.classroom_temas, t["curso_id"])).get(t.get("tema_id", ""), "")
        parcial = g.parcial_de_tema(tema) or parcial_actual()
        carpeta = f"{semestre_actual()}/{materia}/{parcial}/{_nombre_carpeta(t['titulo'])}"
    else:
        carpeta = f"_TELEGRAM/classroom/{_nombre_carpeta(t['curso'])}/{_nombre_carpeta(t['titulo'])}"
    destino = WORKSPACE / carpeta
    archivos, links = [], []
    try:
        archivos, links = await asyncio.to_thread(g.descargar_materiales, t["materiales"], destino)
    except Exception as e:
        log.warning("materiales %s: %s", t["id"], e)
        destino.mkdir(parents=True, exist_ok=True)
    entrega_txt = t["entrega"][:16].replace("T", " ") if t["entrega"] else "sin fecha"
    (destino / "instrucciones.md").write_text(
        f"# {t['titulo']}\n\n- Materia: {materia or t['curso']}\n- Entrega: {entrega_txt}\n- Classroom: {t['link']}\n\n"
        f"## Instrucciones del profe\n{t['descripcion'] or '(sin descripción)'}\n\n"
        f"## Archivos adjuntos\n" + ("\n".join(f"- {a}" for a in archivos) or "(ninguno)") +
        ("\n\n## Links\n" + "\n".join(f"- {l}" for l in links) if links else "") + "\n",
        encoding="utf-8")

    evento_id = None
    if t["entrega"]:
        try:
            ini = datetime.fromisoformat(t["entrega"]) - timedelta(minutes=30)
            evento_id = await asyncio.to_thread(
                g.tarea_agendar, f"📚 Entrega: {t['titulo']} ({materia or t['curso']})", ini.strftime("%Y-%m-%d %H:%M"),
                f"{t['link']}\n\nCarpeta: CETI COLOMOS/{carpeta}", [1440, 180])
        except Exception as e:
            log.warning("evento tarea %s: %s", t["id"], e)

    db.execute("INSERT OR REPLACE INTO tareas(id, curso_id, curso, materia, titulo, descripcion, link, entrega, carpeta,"
               " estado, avisos, pospuesta, evento_id, creada) VALUES (?,?,?,?,?,?,?,?,?,'pendiente','[]',0,?,?)",
               (t["id"], t["curso_id"], t["curso"], materia, t["titulo"], t["descripcion"], t["link"], t["entrega"],
                carpeta, evento_id, time.time()))
    db.commit()
    if not anunciar:
        return
    texto = (f"📚 Nueva tarea de {materia or t['curso']}\n«{t['titulo']}»\n🗓 Entrega: {entrega_txt}\n"
             + (f"\n{t['descripcion'][:600]}\n" if t["descripcion"] else "")
             + f"\n📁 {carpeta}" + (f" ({len(archivos)} adjuntos)" if archivos else "")
             + ("\n📅 Ya está en tu calendario" if evento_id else ""))
    add_msg(ALLOWED_CHAT_ID, "assistant", f"[Aviso de tarea nueva id {t['id']}] {texto}")
    await app.bot.send_message(ALLOWED_CHAT_ID, texto, reply_markup=teclado_tarea(t["id"]))


async def agendar_faltantes():
    """Tareas pendientes que se quedaron sin evento (p. ej. el calendario no estaba conectado)."""
    for t in tareas_pendientes():
        if t["evento_id"] or not t["entrega"] or datetime.fromisoformat(t["entrega"]) < jarvis.ahora():
            continue
        try:
            ini = datetime.fromisoformat(t["entrega"]) - timedelta(minutes=30)
            ev_id = await asyncio.to_thread(
                g.tarea_agendar, f"📚 Entrega: {t['titulo']} ({t['materia'] or t['curso']})", ini.strftime("%Y-%m-%d %H:%M"),
                f"{t['link']}\n\nCarpeta: CETI COLOMOS/{t['carpeta']}", [1440, 180])
            db.execute("UPDATE tareas SET evento_id=? WHERE id=?", (ev_id, t["id"]))
            db.commit()
        except Exception as e:
            log.warning("agendar %s: %s", t["id"], e)


async def ignorar_curso(nombre: str, ignorar: bool = True) -> str:
    c = await _curso(nombre)
    lista = [x for x in cursos_ignorados() if norm(x) != norm(c["nombre"])]
    if ignorar:
        lista.append(c["nombre"])
        # sus tareas pendientes dejan de insistir y se quitan del calendario (amarillo o rojo)
        for t in tareas_pendientes():
            if t["curso_id"] == c["id"]:
                db.execute("UPDATE tareas SET estado='ignorada' WHERE id=?", (t["id"],))
                if t["evento_id"]:
                    try:
                        cuenta = g.CUENTA_CLASSROOM
                        for cal in (g.calendario_pendientes(cuenta), g.calendario_vencidas(cuenta)):
                            try:
                                await asyncio.to_thread(lambda: g._svc(cuenta, "calendar", "v3").events().delete(
                                    calendarId=cal, eventId=t["evento_id"]).execute())
                                break
                            except Exception:
                                continue
                    except Exception as e:
                        log.warning("quitar evento %s: %s", t["id"], e)
        db.commit()
        invalidar_agenda()
    kv_set("cursos_ignorados", json.dumps(lista, ensure_ascii=False))
    return f"{'Ignorando' if ignorar else 'Vuelvo a revisar'} la clase «{c['nombre']}». Ignoradas: {', '.join(lista) or 'ninguna'}"


def cursos_ignorados() -> list[str]:
    return json.loads(kv_get("cursos_ignorados", "[]"))


def curso_ignorado(nombre_curso: str) -> bool:
    ign = {norm(x) for x in cursos_ignorados()}
    return norm(nombre_curso) in ign or (mapear_materia(nombre_curso) or "\0") in cursos_ignorados()


async def revisar_classroom(app: Application):
    await agendar_faltantes()
    primera = kv_get("classroom_inicializado") != "1"
    ahora = jarvis.ahora()
    nuevas = []
    for curso in await asyncio.to_thread(g.classroom_cursos):
        if curso_ignorado(curso["nombre"]):
            continue  # materia exentada / que ya no lleva
        for t in await asyncio.to_thread(g.classroom_tareas, curso["id"], curso["nombre"]):
            if db.execute("SELECT 1 FROM tareas WHERE id=?", (t["id"],)).fetchone():
                continue
            vencida = t["entrega"] and datetime.fromisoformat(t["entrega"]) < ahora
            estado = await asyncio.to_thread(g.classroom_estado, curso["id"], t["id"])
            if vencida or estado in ("TURNED_IN", "RETURNED") or (primera and not t["entrega"]):
                # historial viejo o ya entregada: se anota para no volver a avisar
                db.execute("INSERT OR IGNORE INTO tareas(id, curso_id, curso, titulo, entrega, estado, creada)"
                           " VALUES (?,?,?,?,?,'hecha',?)", (t["id"], curso["id"], curso["nombre"], t["titulo"], t["entrega"], time.time()))
                db.commit()
                continue
            await registrar_tarea(app, t, anunciar=not primera)
            nuevas.append(t)
    if primera:
        kv_set("classroom_inicializado", "1")
        if nuevas:
            lista = "\n".join(f"• {mapear_materia(t['curso']) or t['curso']}: {t['titulo']} — "
                              f"{t['entrega'][:16].replace('T', ' ') if t['entrega'] else 'sin fecha'}" for t in nuevas)
            await app.bot.send_message(ALLOWED_CHAT_ID,
                f"📚 Ya me conecté a tu Classroom. Tienes {len(nuevas)} tareas pendientes, ya las puse en tu "
                f"calendario y bajé los adjuntos:\n\n{lista}\n\nDe aquí en adelante te aviso cuando suban algo nuevo 👀")


def _momentos_aviso(entrega: datetime) -> list[tuple[str, datetime]]:
    m = [("vispera", (entrega - timedelta(days=1)).replace(hour=18, minute=0))]
    for h in (8, 13, 17):
        d = entrega.replace(hour=h, minute=0)
        if d < entrega - timedelta(hours=2):
            m.append((f"dia{h}", d))
    m.append(("2h", entrega - timedelta(hours=2)))
    m.append(("vencida", entrega + timedelta(minutes=5)))
    return m


async def insistir_tareas(app: Application):
    ahora = jarvis.ahora()
    for t in tareas_pendientes():
        if not t["entrega"] or (t["pospuesta"] or 0) > time.time():
            continue
        entrega = datetime.fromisoformat(t["entrega"])
        avisados = set(json.loads(t["avisos"] or "[]"))
        tocan = [k for k, cuando in _momentos_aviso(entrega) if cuando <= ahora and k not in avisados]
        if not tocan:
            continue
        db.execute("UPDATE tareas SET avisos=? WHERE id=?", (json.dumps(sorted(avisados | set(tocan))), t["id"]))
        db.commit()
        try:
            estado = await asyncio.to_thread(g.classroom_estado, t["curso_id"], t["id"])
        except Exception:
            estado = ""
        if estado in ("TURNED_IN", "RETURNED"):
            await marcar_tarea_hecha(t["id"])
            await mensaje_proactivo(app, f"Viste en Classroom que Chava ya entregó «{t['titulo']}» "
                                         f"({t['materia'] or t['curso']}). Felicítalo corto, a tu estilo.", forzar=True)
            continue
        cual = tocan[-1]
        falta = entrega - ahora
        horas = int(falta.total_seconds() // 3600)
        situacion = {
            "vispera": "La entrega es MAÑANA. Pregúntale si ya la hizo.",
            "vencida": "YA SE VENCIÓ y en Classroom no aparece entregada. Pregúntale si la entregó por otro lado o si todavía la puede mandar tarde.",
        }.get(cual, f"Se entrega HOY (faltan ~{horas} h) y en Classroom sigue sin entregar. Chíngale para que no procrastine: "
                    "cada vez que le insistas hoy sube un poquito el tono (sin ser ofensivo de verdad).")
        await mensaje_proactivo(app,
            f"Tarea «{t['titulo']}» de {t['materia'] or t['curso']}, entrega {entrega:%Y-%m-%d %H:%M}. {situacion} "
            "Recuérdale que abajo tiene botones para marcarla o para que Claude se la haga. Corto, 1-3 líneas.",
            reply_markup=teclado_tarea(t["id"], vencida=cual == "vencida"), forzar=True)


DIAS_GRACIA_VENCIDA = 3  # días después de la entrega en que todavía se revisa si la entregó tarde


async def pintar_vencidas():
    """En cuanto pasa la hora de entrega: si Classroom no la tiene entregada, su evento pasa de 🟡 a 🔴."""
    ahora = jarvis.ahora()
    for t in tareas_pendientes():
        if not t["entrega"] or not t["evento_id"] or datetime.fromisoformat(t["entrega"]) > ahora:
            continue
        if db.execute("SELECT en_rojo FROM tareas WHERE id=?", (t["id"],)).fetchone()[0]:
            continue
        try:
            estado = await asyncio.to_thread(g.classroom_estado, t["curso_id"], t["id"])
        except Exception:
            continue
        if estado in ("TURNED_IN", "RETURNED"):
            await marcar_tarea_hecha(t["id"])
            continue
        if await asyncio.to_thread(g.evento_tarea_vencida, t["evento_id"]):
            db.execute("UPDATE tareas SET en_rojo=1 WHERE id=?", (t["id"],))
            db.commit()
            invalidar_agenda()
            log.info("Tarea vencida → calendario rojo: %s", t["titulo"])


async def cerrar_vencidas():
    """Tareas cuya entrega pasó hace más de DIAS_GRACIA_VENCIDA días: si en Classroom aparece entregada
    → 'hecha'; si no → 'vencida'. Así dejan de salir como pendientes para siempre (en /tareas, en el
    system prompt y en los resúmenes). Los botones siguen funcionando: 'Ya la entregué' la pasa a 'hecha'."""
    limite = jarvis.ahora() - timedelta(days=DIAS_GRACIA_VENCIDA)
    for t in tareas_pendientes():
        if not t["entrega"] or datetime.fromisoformat(t["entrega"]) > limite:
            continue
        try:
            estado = await asyncio.to_thread(g.classroom_estado, t["curso_id"], t["id"])
        except Exception as e:
            log.warning("estado de vencida %s: %s", t["id"], e)
            continue  # sin respuesta de Classroom se reintenta en la siguiente vuelta
        if estado in ("TURNED_IN", "RETURNED"):
            await marcar_tarea_hecha(t["id"])
        else:
            db.execute("UPDATE tareas SET estado='vencida' WHERE id=?", (t["id"],))
            db.commit()
            log.info("Tarea vencida sin entregar: %s", t["titulo"])


# ---------- Anuncios de Classroom → resumen + botón para agendar fechas ----------

def grupo_alumno() -> str:
    """Grupo del datos.md del semestre activo (p. ej. 'I-1Me')."""
    try:
        t = (WORKSPACE / semestre_actual() / "Z99_Datos_documentos" / "datos.md").read_text(encoding="utf-8")
        return re.search(r"^\s*-\s*grupo:\s*(.+)$", t, re.M | re.I)[1].strip()
    except Exception:
        return ""


def variantes_grupo(g_: str) -> str:
    """'I-1Me' → 'I-1Me, 1Me, 1M, I-1M, 1ME'."""
    base = re.sub(r"^[A-Za-z]-", "", g_)
    corto = re.sub(r"(?<=\d[A-Za-z])[a-z]+$", "", base)
    return ", ".join(dict.fromkeys(x for x in (g_, base, corto, g_.replace(base, corto), base.upper()) if x))


PROMPT_ANUNCIO = """Eres el asistente de un estudiante. Un profesor publicó este anuncio en Classroom.
El alumno es del GRUPO {grupo} (también se escribe {variantes}). Si el anuncio da fechas distintas por grupo,
pon SOLO las de su grupo e ignora las de otros grupos (menciónalo en el resumen).
Materia: {materia}
Publicado: {fecha}
{fechas}

Anuncio:
\"\"\"{texto}\"\"\"

Devuelve SOLO un JSON: {{"resumen": "1-2 líneas, en español, qué dijo y qué tiene que hacer el alumno",
 "importante": true/false, "fechas": [{{"titulo": "corto, p. ej. 'Examen 2º parcial Discretas'",
 "tipo": "examen|entrega|exposicion|clase|evento|otro", "inicio": "AAAA-MM-DD" o "AAAA-MM-DD HH:MM",
 "fin": "" o "AAAA-MM-DD HH:MM"}}]}}
Solo pon fechas que el anuncio diga de verdad (resuelve 'el próximo lunes', 'mañana' con la fecha de publicación).
Si no hay fechas, "fechas": []."""


def teclado_anuncio(aid: str, fechas: list[dict]) -> InlineKeyboardMarkup | None:
    filas = [[InlineKeyboardButton(f"📅 {f['titulo'][:26]} · {f['inicio'][5:10].replace('-', '/')}"
                                   + (f" {f['inicio'][11:16]}" if len(f['inicio']) > 10 else ""),
                                   callback_data=f"anu:{aid[-20:]}:{i}")] for i, f in enumerate(fechas[:6])
             if not f.get("agregado")]  # el índice es el de la lista completa: no cambia al ir agregando
    if len(filas) > 1:
        filas.append([InlineKeyboardButton("📅 Agregar todas", callback_data=f"anu:{aid[-20:]}:all")])
    return InlineKeyboardMarkup(filas) if filas else None


async def procesar_anuncio(app: Application, curso: dict, a: dict):
    materia = mapear_materia(curso["nombre"]) or curso["nombre"]
    try:
        resp = await deepseek_chat(
            "anuncio", model=DEEPSEEK_MODEL, response_format={"type": "json_object"}, temperature=0.2,
            messages=[{"role": "user", "content": PROMPT_ANUNCIO.format(
                materia=materia, fecha=a["fecha"], texto=a["texto"], fechas=tabla_fechas(jarvis.ahora()),
                grupo=grupo_alumno() or "?", variantes=variantes_grupo(grupo_alumno()))}])
        info = json.loads(resp.choices[0].message.content)
    except Exception:
        log.exception("anuncio %s", a["id"])
        info = {"resumen": "", "fechas": []}
    fechas = [f for f in info.get("fechas", []) if f.get("titulo") and re.match(r"\d{4}-\d{2}-\d{2}", f.get("inicio", ""))]
    db.execute("INSERT OR REPLACE INTO anuncios(id, curso, fecha, texto, fechas, visto) VALUES (?,?,?,?,?,?)",
               (a["id"], materia, a["fecha"], a["texto"], json.dumps(fechas, ensure_ascii=False), time.time()))
    db.commit()
    texto = (f"📣 <b>{html.escape(materia)}</b> · {a['fecha'][5:].replace('-', '/')}\n"
             + (f"<b>{html.escape(info.get('resumen', ''))}</b>\n\n" if info.get("resumen") else "")
             + f"<i>{html.escape(a['texto'][:1500])}</i>"
             + (f"\n\n<a href=\"{html.escape(a['link'], quote=True)}\">Abrir en Classroom</a>" if a.get("link") else ""))
    await app.bot.send_message(ALLOWED_CHAT_ID, texto[:4000], parse_mode="HTML", disable_web_page_preview=True,
                               reply_markup=teclado_anuncio(a["id"], fechas))
    add_msg(ALLOWED_CHAT_ID, "assistant", f"[Anuncio de {materia} ({a['fecha']}): {a['texto'][:600]}"
            + (f" — fechas detectadas: {', '.join(f['titulo'] + ' ' + f['inicio'] for f in fechas)}" if fechas else "") + "]")


async def revisar_anuncios(app: Application):
    if "classroom.announcements.readonly" in g.permisos_faltantes_cache(g.CUENTA_CLASSROOM):
        return
    primera = kv_get("anuncios_inicializado") != "1"
    limite = (jarvis.ahora() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    for curso in await asyncio.to_thread(g.classroom_cursos):
        if curso_ignorado(curso["nombre"]):
            continue
        for a in reversed(await asyncio.to_thread(g.classroom_anuncios, curso["id"], 10)):
            if db.execute("SELECT 1 FROM anuncios WHERE id=?", (a["id"],)).fetchone():
                continue
            if primera and a["fecha"] < limite:  # la primera vez solo lo de las últimas 48 h
                db.execute("INSERT OR IGNORE INTO anuncios(id, curso, fecha, texto, visto) VALUES (?,?,?,?,?)",
                           (a["id"], curso["nombre"], a["fecha"], a["texto"], time.time()))
                continue
            await procesar_anuncio(app, curso, a)
    db.commit()
    kv_set("anuncios_inicializado", "1")


async def job_classroom(ctx: ContextTypes.DEFAULT_TYPE):
    """Busca tareas nuevas en Classroom (cada CLASSROOM_CADA_MIN)."""
    if not g.configurado() or g.CUENTA_CLASSROOM not in g.conectadas():
        return
    try:
        await revisar_classroom(ctx.application)
    except Exception:
        log.exception("revisar classroom")
    try:
        await pintar_vencidas()
    except Exception:
        log.exception("pintar vencidas")
    try:
        await cerrar_vencidas()
    except Exception:
        log.exception("cerrar vencidas")
    try:
        await revisar_anuncios(ctx.application)
    except Exception:
        log.exception("revisar anuncios")


async def job_insistir(ctx: ContextTypes.DEFAULT_TYPE):
    """Avisos de entrega (víspera, día, 2 h antes, vencida). Va aparte y más seguido que job_classroom:
    solo lee la base local y únicamente consulta Classroom/DeepSeek cuando toca un aviso."""
    if not g.configurado() or g.CUENTA_CLASSROOM not in g.conectadas():
        return
    try:
        await insistir_tareas(ctx.application)
    except Exception:
        log.exception("insistir tareas")


def programar_recordatorio(app: Application, item: dict):
    cuando = datetime.fromisoformat(item["cuando"])
    if cuando < jarvis.ahora():
        cuando = jarvis.ahora() + timedelta(seconds=30)  # se pasó mientras el bot estaba apagado
    app.job_queue.run_once(job_recordatorio, when=cuando, data=item, name=f"rec-{item['id']}")


def _hora(hhmm: str) -> dtime | None:
    if not hhmm.strip():
        return None
    h, m = hhmm.split(":")
    return dtime(int(h), int(m), tzinfo=jarvis.TZ)


# ---------- Eventos de software en Guadalajara (búsqueda diaria con Claude) ----------

# La búsqueda gasta límite de Claude (ventana de 5 h): corre de MADRUGADA para que la ventana ya se haya
# renovado cuando Chava despierta, solo algunos días, con el modelo económico y sin sub-agentes.
HORA_EVENTOS = os.environ.get("HORA_EVENTOS", "01:00")
DIAS_EVENTOS = os.environ.get("DIAS_EVENTOS", "lu,ju")          # días en que busca
HORA_AVISO_EVENTOS = os.environ.get("HORA_AVISO_EVENTOS", "08:10")  # cuándo te manda los nuevos
MODELO_EVENTOS = os.environ.get("MODELO_EVENTOS", "claude-haiku-4-5")
TRABAJO_BARATO = {"model": MODELO_EVENTOS, "max_turns": 30, "disallowed_tools": ["Task", "Agent", "Skill"]}
EVENTOS_DIR = jarvis.BASE / "eventos"
EVENTOS_JSON = EVENTOS_DIR / "eventos.json"      # todo lo que se ha encontrado (con su estado)
BUSQUEDA_JSON = EVENTOS_DIR / "busqueda.json"    # lo que deja Claude en cada búsqueda

PROMPT_EVENTOS = """Investiga en internet eventos PRESENCIALES de tecnología/software en el área metropolitana de
Guadalajara, Jalisco (Guadalajara, Zapopan, Tlaquepaque, Tonalá) de HOY ({hoy}) a {hasta}, para Salvador:
estudiante de ingeniería en el CETI Colomos que programa y busca trabajo/prácticas en tech.

Busca a fondo (WebSearch + WebFetch) y verifica cada evento en su página oficial. Tipos:
- meetups y comunidades (GDG Guadalajara, Python/JS/.NET/Java/Rust GDL, AWS User Group GDL, Azure/Microsoft,
  Women Techmakers, GDSC/universidades, Hackers & Founders, Epicentro, ciberseguridad/DEF CON groups, IA/datos)
- conferencias y expos (Jalisco Talent Land, Campus Party, Tech Week, eventos de Intel/Oracle/IBM/HP/Oracle GDL)
- hackatones y concursos (incluye MLH, Devpost, gobierno de Jalisco)
- ferias de empleo y reclutamiento tech / prácticas / becas (universidades: CETI, UdeG/CUCEI, ITESO, Tec GDL,
  UP, UAG; Servicio Nacional de Empleo Jalisco; empresas)
- talleres, bootcamps y cursos GRATIS o baratos de programación
Fuentes: meetup.com, lu.ma, eventbrite.com.mx, sitios de comunidades, LinkedIn Events, Facebook, sitios de
universidades y del Gobierno de Jalisco / Secretaría de Innovación.

Eventos que ya conozco (no hace falta re-investigarlos, pero inclúyelos si siguen vigentes y tienes datos nuevos):
{conocidos}

PRESUPUESTO: trabaja TÚ SOLO, sin sub-agentes ni skills. Máximo ~12 búsquedas y ~12 páginas abiertas; ve directo
a las fuentes de la lista. Es mejor traer 8 eventos bien verificados que agotar todas las fuentes.
Reglas: NO inventes nada. Solo eventos con fecha concreta y link verificable. Si la fecha u hora no son seguras,
dilo en "notas". Prioriza lo gratis y lo útil para un estudiante.

Escribe el resultado en /jarvis/eventos/busqueda.json (sobrescríbelo) con este formato exacto:
{{"generado": "{hoy}", "eventos": [{{
  "nombre": "...", "tipo": "meetup|conferencia|hackaton|feria de empleo|taller|expo|otro",
  "inicio": "AAAA-MM-DD HH:MM" (o "AAAA-MM-DD" si no hay hora), "fin": "AAAA-MM-DD HH:MM" o "",
  "lugar": "lugar y dirección", "organizador": "...", "costo": "Gratis | $… | Sin dato",
  "link": "https://...", "resumen": "1-2 líneas de qué es y por qué le sirve", "notas": ""}}]}}
No crees ni modifiques ningún otro archivo. Tu respuesta final: cuántos eventos encontraste, en una línea."""


def _evt_id(e: dict) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, norm(e.get("nombre", "")) + "|" + (e.get("inicio") or "")[:10]).hex[:10]


def eventos_cargar() -> list[dict]:
    try:
        return json.loads(EVENTOS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []


def eventos_guardar(evs: list[dict]):
    EVENTOS_DIR.mkdir(parents=True, exist_ok=True)
    EVENTOS_JSON.write_text(json.dumps(evs, ensure_ascii=False, indent=1), encoding="utf-8")


def eventos_proximos(dias: int = 30) -> list[dict]:
    hoy = jarvis.ahora().strftime("%Y-%m-%d")
    lim = (jarvis.ahora() + timedelta(days=dias)).strftime("%Y-%m-%d")
    return sorted((e for e in eventos_cargar() if hoy <= (e.get("inicio") or "")[:10] <= lim
                   and e.get("estado") != "descartado"), key=lambda e: e["inicio"])


def _evt_linea(i: int, e: dict) -> str:
    cuando = e["inicio"].replace(" ", " · ") if e.get("inicio") else "fecha por confirmar"
    return (f"<b>{i}. {html.escape(e['nombre'])}</b> ({html.escape(e.get('tipo', ''))})\n"
            f"🗓 {cuando} · 📍 {html.escape(e.get('lugar', '')[:80])}\n"
            f"💵 {html.escape(e.get('costo', 'Sin dato'))} · <a href=\"{html.escape(e.get('link', ''), quote=True)}\">link</a>\n"
            f"{html.escape(e.get('resumen', '')[:200])}")


def teclado_eventos(evs: list[dict]) -> InlineKeyboardMarkup | None:
    filas = [[InlineKeyboardButton(f"📅 {i}. {e['nombre'][:28]}", callback_data=f"evt:add:{e['id']}")]
             for i, e in enumerate(evs, 1) if e.get("estado") != "agendado"]
    return InlineKeyboardMarkup(filas[:10]) if filas else None


async def mandar_eventos(bot, evs: list[dict], titulo: str):
    if not evs:
        return
    texto = f"<b>{titulo}</b>\n\n" + "\n\n".join(_evt_linea(i, e) for i, e in enumerate(evs, 1))
    texto += "\n\nPícale al que quieras y te lo meto al calendario 👇"
    await bot.send_message(ALLOWED_CHAT_ID, texto[:4000], parse_mode="HTML", disable_web_page_preview=True,
                           reply_markup=teclado_eventos(evs))


async def buscar_eventos(app: Application, avisar_sin_nuevos: bool = False, silencioso: bool = False):
    """`silencioso`: no manda nada (búsqueda de madrugada); los nuevos se avisan con job_aviso_eventos."""
    hoy = jarvis.ahora()
    conocidos = [f"- {e['nombre']} ({e.get('inicio', '')[:10]})" for e in eventos_proximos(60)]
    prompt = PROMPT_EVENTOS.format(hoy=hoy.strftime("%Y-%m-%d"), hasta=(hoy + timedelta(days=45)).strftime("%Y-%m-%d"),
                                   conocidos="\n".join(conocidos) or "(ninguno)")
    EVENTOS_DIR.mkdir(parents=True, exist_ok=True)
    BUSQUEDA_JSON.unlink(missing_ok=True)
    r = await worker.post("/jobs", json={"prompt": prompt, "cwd": "JARVIS/eventos"} | TRABAJO_BARATO)
    job_id = r.json()["id"]
    while True:
        await asyncio.sleep(20)
        try:
            j = (await worker.get(f"/jobs/{job_id}")).json()
        except Exception:
            continue
        if j.get("status") not in ("queued", "running"):
            break
    try:
        encontrados = json.loads(BUSQUEDA_JSON.read_text(encoding="utf-8")).get("eventos", [])
    except Exception:
        log.warning("búsqueda de eventos sin resultado (%s): %s", j.get("status"), (j.get("result") or "")[:200])
        if avisar_sin_nuevos:
            await app.bot.send_message(ALLOWED_CHAT_ID, "⚠️ La búsqueda de eventos falló esta vez. Mañana lo vuelvo a intentar.")
        return
    todos = {e["id"]: e for e in eventos_cargar()}
    nuevos = []
    for e in encontrados:
        if not e.get("nombre") or not e.get("inicio") or e["inicio"][:10] < hoy.strftime("%Y-%m-%d"):
            continue
        e["id"] = _evt_id(e)
        if e["id"] in todos:
            todos[e["id"]] = todos[e["id"]] | {k: v for k, v in e.items() if v} | {"estado": todos[e["id"]].get("estado")}
        else:
            e |= {"estado": "nuevo", "encontrado": hoy.strftime("%Y-%m-%d"), "avisado": not silencioso}
            todos[e["id"]] = e
            nuevos.append(e)
    eventos_guardar(sorted(todos.values(), key=lambda e: e.get("inicio", "")))
    nuevos.sort(key=lambda e: e["inicio"])
    log.info("Búsqueda de eventos: %d nuevos (costo equivalente $%s)", len(nuevos), j.get("cost_usd"))
    if silencioso:
        return
    if nuevos:
        await mandar_eventos(app.bot, nuevos, f"🗓 {len(nuevos)} evento(s) de software nuevos en Guadalajara")
        add_msg(ALLOWED_CHAT_ID, "assistant", "[Le mandé eventos nuevos de software en GDL: " +
                "; ".join(f"{e['nombre']} ({e['inicio']})" for e in nuevos) + "]")
    elif avisar_sin_nuevos:
        await app.bot.send_message(ALLOWED_CHAT_ID, "🗓 Busqué y no salió nada nuevo. Lo que ya conozco está en /eventos.")


async def job_eventos(ctx: ContextTypes.DEFAULT_TYPE):
    """Búsqueda automática (de madrugada, solo los días de DIAS_EVENTOS, y si no se ha gastado mucho límite)."""
    if not claude_activo():
        return
    hoy = ["lu", "ma", "mi", "ju", "vi", "sa", "do"][jarvis.ahora().weekday()]
    if hoy not in [d.strip()[:2].lower() for d in DIAS_EVENTOS.split(",")]:
        return
    try:
        uso = (await worker.get("/uso", timeout=40)).json()
        if float(uso.get("5h-utilization", 0)) > 0.5:
            log.info("Búsqueda de eventos saltada: ventana de Claude al %.0f%%", float(uso["5h-utilization"]) * 100)
            return
    except Exception:
        pass
    try:
        await buscar_eventos(ctx.application, silencioso=True)
    except Exception:
        log.exception("búsqueda de eventos")


async def job_aviso_eventos(ctx: ContextTypes.DEFAULT_TYPE):
    """En la mañana: manda los eventos que encontró de madrugada."""
    evs = eventos_cargar()
    nuevos = [e for e in evs if e.get("avisado") is False and e.get("inicio", "")[:10] >= jarvis.ahora().strftime("%Y-%m-%d")]
    if not nuevos:
        return
    await mandar_eventos(ctx.bot, nuevos[:10], f"🗓 {len(nuevos)} evento(s) de software nuevos en Guadalajara")
    add_msg(ALLOWED_CHAT_ID, "assistant", "[Le mandé eventos nuevos de software en GDL: " +
            "; ".join(f"{e['nombre']} ({e['inicio']})" for e in nuevos) + "]")
    for e in evs:
        if e.get("avisado") is False:
            e["avisado"] = True
    eventos_guardar(evs)


async def agendar_evento(eid: str) -> str:
    evs = eventos_cargar()
    e = next((x for x in evs if x["id"] == eid), None)
    if not e:
        raise ValueError("ese evento ya no existe")
    inicio = e["inicio"] if len(e["inicio"]) > 10 else e["inicio"][:10]
    await asyncio.to_thread(
        g.cal_crear, f"💻 {e['nombre']}", inicio, e.get("fin") or "", 120,
        f"{e.get('resumen', '')}\n\nOrganiza: {e.get('organizador', '')}\nCosto: {e.get('costo', '')}\n{e.get('link', '')}",
        e.get("lugar", ""), "", [1440, 120], "morado")
    e["estado"] = "agendado"
    eventos_guardar(evs)
    invalidar_agenda()
    return e["nombre"]


def contexto_eventos() -> str:
    evs = eventos_proximos(21)[:10]
    if not evs:
        return ""
    return "# Eventos de software en GDL que encontraste (próximas 3 semanas)\n" + "\n".join(
        f"- {e['nombre']} — {e['inicio']} — {e.get('lugar', '')[:50]} ({e.get('estado')}) {e.get('link', '')}" for e in evs)


# ---------- Comandos de estado ----------

def _hace(ts: str) -> str:
    try:
        return datetime.fromtimestamp(int(ts), jarvis.TZ).strftime("%a %d %b %H:%M")
    except Exception:
        return "?"


# ---------- Impresora de la casa (solo con /print on, y siempre confirmando con botón) ----------

def print_activo() -> bool:
    return kv_get("print_activo", "0") == "1"


def _dlg_texto(d: dict) -> str:
    est = d.get("estado") or {}
    avisos = " · ".join(f"⚠️ {a}" for a in est.get("avisos", []))
    pag = f"{d['npag']} pág." if d.get("npag") else "páginas: se sabrá al convertir"
    hojas = f" → {d['npag'] * d['copias']} hojas" if d.get("npag") and not d["paginas"] else ""
    return (f"🖨 <b>Imprimir</b>: {html.escape(Path(d['ruta']).name)}\n"
            f"{pag}{hojas}" + (f" · solo páginas {html.escape(d['paginas'])}" if d["paginas"] else "") + "\n\n"
            f"Color: <b>{'🎨 a color' if d['color'] else '⚫ blanco y negro'}</b>\n"
            f"Copias: <b>{d['copias']}</b> · Calidad: <b>{d['calidad']}</b> · Tamaño: <b>{d['tamano']}</b>\n\n"
            f"Impresora: {est.get('texto', '?')}" + (f"\n{avisos}" if avisos else ""))


def _dlg_teclado(key: str, d: dict) -> InlineKeyboardMarkup:
    b = lambda texto, acc: InlineKeyboardButton(texto, callback_data=f"prt:{key}:{acc}")
    ok = lambda cond: "✅ " if cond else ""
    return InlineKeyboardMarkup([
        [b(ok(d["color"]) + "🎨 Color", "c1"), b(ok(not d["color"]) + "⚫ B/N", "c0")],
        [b("➖", "m"), b(f"Copias: {d['copias']}", "nada"), b("➕", "p")],
        [b(ok(d["calidad"] == q) + q.capitalize(), f"q-{q}") for q in ("borrador", "normal", "alta")],
        [b(ok(d["tamano"] == t) + t.capitalize(), f"t-{t}") for t in ("carta", "oficio", "a4")],
        [b("🖨 IMPRIMIR", "go"), b("❌ Cancelar", "x")],
    ])


async def abrir_dialogo_impresion(ctx: ContextTypes.DEFAULT_TYPE, chat: int, ruta: Path, copias: int = 1,
                                  color: bool = True, calidad: str = "normal", paginas: str = "", tamano: str = "carta") -> str:
    if not print_activo():
        await ctx.bot.send_message(chat, "🖨 La impresión está apagada. Préndela con /print on.")
        return "apagada"
    if not impresora.imprimible(ruta):
        await ctx.bot.send_message(chat, f"No sé imprimir archivos {ruta.suffix or 'sin extensión'}. Sí puedo: PDF, Word, "
                                         "PowerPoint, Excel, imágenes (jpg/png) y texto.")
        return "formato"
    key = uuid.uuid4().hex[:8]
    d = {"ruta": str(ruta), "copias": max(1, min(20, int(copias or 1))), "color": bool(color),
         "calidad": calidad if calidad in impresora.CALIDADES else "normal", "paginas": paginas or "",
         "tamano": tamano if tamano in impresora.TAMANOS else "carta",
         "npag": await asyncio.to_thread(impresora.paginas, ruta),
         "estado": await asyncio.to_thread(impresora.estado)}
    ctx.bot_data.setdefault("impresiones", {})[key] = d
    await ctx.bot.send_message(chat, _dlg_texto(d), parse_mode="HTML", reply_markup=_dlg_teclado(key, d))
    return "ok"


async def ejecutar_impresion(ctx: ContextTypes.DEFAULT_TYPE, chat: int, d: dict, msg) -> None:
    ruta = Path(d["ruta"])
    try:
        if ruta.suffix.lower() in impresora.CONVERTIBLES:  # Word/PowerPoint/Excel → PDF con LibreOffice (worker, sin Claude)
            await msg.edit_text(f"📄 Convirtiendo {html.escape(ruta.name)} a PDF…", parse_mode="HTML")
            rel = ("JARVIS/" + str(ruta.relative_to(jarvis.BASE))) if ruta.is_relative_to(jarvis.BASE) else str(ruta.relative_to(WORKSPACE))
            r = await worker.post("/pdf", json={"ruta": rel}, timeout=200)
            if r.status_code != 200:
                raise RuntimeError(f"no pude convertirlo a PDF: {r.text[:200]}")
            ruta = WORKSPACE / r.json()["pdf"]
        trabajo = await asyncio.to_thread(impresora.imprimir, ruta, d["copias"], d["color"], d["calidad"], d["tamano"], d["paginas"])
    except Exception as e:
        log.exception("imprimir")
        await msg.edit_text(f"⚠️ No se pudo imprimir: {html.escape(str(e))}", parse_mode="HTML")
        return
    resumen = (f"{html.escape(Path(d['ruta']).name)} · {'color' if d['color'] else 'B/N'} · {d['copias']} copia(s) · "
               f"{d['calidad']} · {d['tamano']}")
    await msg.edit_text(f"🖨 Mandado a imprimir…\n{resumen}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancelar impresión", callback_data=f"prs:{trabajo}")]]))
    add_msg(chat, "user", f"[Mandé a imprimir {Path(d['ruta']).name}: {'color' if d['color'] else 'blanco y negro'}, "
                          f"{d['copias']} copia(s), calidad {d['calidad']}]")
    avisado = False
    for i in range(90):  # hasta ~7.5 min
        await asyncio.sleep(5)
        if not await asyncio.to_thread(impresora.pendiente, trabajo):
            try:
                await msg.edit_text(f"✅ Impreso\n{resumen}", parse_mode="HTML")
            except Exception:
                pass
            return
        if not avisado and i in (5, 17, 40):  # a los ~30 s, 1.5 min y 3.5 min revisa si la impresora se queja
            avisos = (await asyncio.to_thread(impresora.estado)).get("avisos", [])
            problemas = [a for a in avisos if "tinta/papel" not in a]
            if problemas:
                avisado = True
                await ctx.bot.send_message(chat, "🖨 La impresora dice: " + ", ".join(problemas) +
                                           ". En cuanto lo arregles sigue sola (o cancela con el botón).")
    await ctx.bot.send_message(chat, "🖨 La impresión sigue en cola después de varios minutos. Revisa la impresora; "
                                     "puedes cancelarla con el botón de arriba.")


def boton_imprimir(ctx: ContextTypes.DEFAULT_TYPE, ruta: Path) -> InlineKeyboardMarkup | None:
    """Botón 🖨 debajo de un archivo que se le manda a Chava (solo si /print on)."""
    if not print_activo() or not impresora.imprimible(ruta):
        return None
    token = uuid.uuid4().hex[:10]
    ctx.bot_data.setdefault("imprimibles", {})[token] = str(ruta)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🖨 Imprimir", callback_data=f"prn:{token}")]])


def _archivo_para_imprimir(texto: str) -> Path:
    try:
        return resolver_ruta(texto)
    except ValueError:
        p = _entrada() / Path(texto).name
        if p.is_file():
            return p
        raise


def texto_print_estado(est: dict | None = None) -> str:
    cab = "🟢 <b>Impresión: prendida</b>" if print_activo() else "🔴 <b>Impresión: apagada</b> (nadie puede imprimir)"
    if est is None:
        return cab
    tinta = " · ".join(f"{n} {v}%" for n, v in est.get("tinta", {}).items() if v is not None)
    return (cab + f"\n\n🖨 HP Smart Tank 210-220: <b>{est['texto']}</b>"
            + (f"\nTinta: {tinta}" if tinta else "")
            + "".join(f"\n⚠️ {a}" for a in est.get("avisos", []))
            + "\n\nPara imprimir: mándame un archivo con el pie <code>/print</code>, responde a un archivo con "
              "<code>/print</code>, o dime <i>\"imprime la tarea de Discretas a color\"</i>.")


def teclado_print() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("✅ " if print_activo() else "") + "Prendida", callback_data="pon:1"),
        InlineKeyboardButton(("✅ " if not print_activo() else "") + "Apagada", callback_data="pon:0")]])


async def cmd_print(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/print on|off · /print (estado) · responder a un archivo con /print → lo imprime (con diálogo)."""
    if not autorizado(update):
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("on", "prender", "encender"):
        kv_set("print_activo", "1")
    elif arg in ("off", "apagar"):
        kv_set("print_activo", "0")
    citado = update.message.reply_to_message
    if not arg and citado and (citado.document or citado.photo):
        if citado.document:
            tg_file, nombre = await citado.document.get_file(), citado.document.file_name or "archivo"
        else:
            tg_file, nombre = await citado.photo[-1].get_file(), "foto.jpg"
        destino = _entrada() / f"{time.strftime('%Y%m%d_%H%M%S')}_{Path(nombre).name}"
        destino.parent.mkdir(parents=True, exist_ok=True)
        await tg_file.download_to_drive(destino)
        await abrir_dialogo_impresion(ctx, update.effective_chat.id, destino)
        return
    est = await asyncio.to_thread(impresora.estado)
    await update.effective_chat.send_message(texto_print_estado(est), parse_mode="HTML", reply_markup=teclado_print())


def texto_tinta(est: dict) -> str:
    """Estado de la impresora con barras de tinta y papel (funciona aunque /print esté apagado: solo lee)."""
    emoji = {"negro": "⚫", "cian": "🔵", "magenta": "🟣", "amarillo": "🟡"}
    lineas = [f"🖨 <b>HP Smart Tank 210-220</b>: {est['texto']}"]
    if est.get("tinta"):
        lineas.append("\n<b>Tinta</b>")
        for nombre, v in est["tinta"].items():
            if v is None:
                lineas.append(f"{emoji.get(nombre, '▫️')} {nombre}: sin dato")
                continue
            barra = "▓" * round(v / 10) + "░" * (10 - round(v / 10))
            lineas.append(f"{emoji.get(nombre, '▫️')} {barra} {v}% {nombre}" + (" ⚠️ baja" if v <= 15 else ""))
    else:
        lineas.append("\nTinta: no pude leerla (¿impresora apagada o cambió de IP?)")
    avisos = est.get("avisos", [])
    sin_papel = any("papel" in a for a in avisos)
    lineas.append("\n<b>Papel</b>: " + ("⚠️ " + next(a for a in avisos if "papel" in a) if sin_papel
                                         else "✅ hay papel en la bandeja" if est.get("tinta") else "sin dato"))
    otros = [a for a in avisos if "papel" not in a]
    lineas += [f"⚠️ {a}" for a in otros]
    lineas.append("\n<i>La impresora solo avisa si la bandeja está vacía, no cuántas hojas quedan. "
                  "Los tanques los reporta ella misma; confírmalo viendo las ventanitas.</i>")
    return "\n".join(lineas)


async def cmd_tinta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    est = await asyncio.to_thread(impresora.estado)
    await update.effective_chat.send_message(texto_tinta(est), parse_mode="HTML")


IMPRESORA_TOOLS = [{"type": "function", "function": {
    "name": "impresora_estado",
    "description": "Estado de la impresora de la casa: si está lista, nivel de tinta por color (%), si hay papel y avisos (atasco, tapa abierta). Solo lee; funciona aunque la impresión esté apagada.",
    "parameters": {"type": "object", "properties": {}}}}, {"type": "function", "function": {
    "name": "imprimir",
    "description": ("Abre el diálogo de impresión de un archivo en la impresora de su casa (HP Smart Tank: color o B/N, una "
                    "cara). Chava elige las opciones con botones y confirma; tú NO imprimes directo ni digas que ya se "
                    "imprimió. Pasa lo que él ya te haya dicho (color, copias…) para que salga preseleccionado."),
    "parameters": {"type": "object", "properties": {
        "archivo": {"type": "string", "description": "Ruta ('CETI COLOMOS/SEMESTRE_1/…/x.pdf', 'JARVIS/archivos/…') o el nombre de un archivo que mandó por Telegram."},
        "color": {"type": "boolean", "description": "true = a color, false = blanco y negro."},
        "copias": {"type": "integer"},
        "calidad": {"type": "string", "enum": ["borrador", "normal", "alta"]},
        "tamano": {"type": "string", "enum": ["carta", "oficio", "a4"]},
        "paginas": {"type": "string", "description": "Opcional, p. ej. '1-3,5'."}},
        "required": ["archivo"]}}}]


async def tool_imprimir(ctx: ContextTypes.DEFAULT_TYPE, chat: int, a: dict) -> str:
    if not print_activo():
        return "La impresión está APAGADA (/print off). Dile que la prenda con /print on si quiere imprimir."
    ruta = _archivo_para_imprimir(a["archivo"])
    r = await abrir_dialogo_impresion(ctx, chat, ruta, a.get("copias") or 1, a.get("color", True),
                                      a.get("calidad", "normal"), a.get("paginas", ""), a.get("tamano", "carta"))
    return ("Le mostré el diálogo de impresión con botones (color/B-N, copias, calidad). Aún NO se imprime: él confirma."
            if r == "ok" else f"No se abrió el diálogo ({r}).")


def texto_claude_estado() -> str:
    if claude_activo():
        return ("🟢 <b>Claude: prendido</b>\nPuedo mandarle tareas y gasta tu límite del plan (/uso).\n\n"
                "Uso directo: <code>/claude lo que quieres</code> · <code>/claude personal …</code>")
    return ("🔴 <b>Claude: apagado</b>\nNada gasta tu límite: ni tareas, ni /claude, ni la búsqueda de eventos. "
            "Yo sigo normal para platicar, calendario, correos, Classroom y recordatorios.")


def teclado_claude() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("✅ " if claude_activo() else "") + "Prendido", callback_data="cla:1"),
        InlineKeyboardButton(("✅ " if not claude_activo() else "") + "Apagado", callback_data="cla:0")]])


async def cmd_claude(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/claude <pedido> → va directo a Claude Code, sin pasar por DeepSeek.
    /claude personal <pedido> → trabaja en tu espacio personal (JARVIS/archivos)."""
    if not autorizado(update):
        return
    texto = (update.message.text or "").split(maxsplit=1)[1].strip() if len((update.message.text or "").split(maxsplit=1)) > 1 else ""
    if texto.lower() in ("on", "off", "prender", "apagar", "encender", "estado", ""):
        if texto.lower() in ("on", "prender", "encender"):
            kv_set("claude_activo", "1")
        elif texto.lower() in ("off", "apagar"):
            kv_set("claude_activo", "0")
        if texto or not update.message.reply_to_message:
            await update.effective_chat.send_message(texto_claude_estado(), parse_mode="HTML", reply_markup=teclado_claude())
            return
    if ES_SKILLS_ADD.match(texto):
        await instalar_skills(update.effective_chat, texto)
        return
    citado = update.message.reply_to_message
    if citado and (citado.text or citado.caption):
        texto += f"\n\n(Mensaje citado: {(citado.text or citado.caption)[:3000]})"
    if not texto:
        await update.effective_chat.send_message(
            "Uso: <code>/claude lo que quieres</code>\n"
            "Ej: <code>/claude hazme un resumen de Mercosur para Cultura Comparada P2 en Word con portada</code>\n"
            "<code>/claude personal</code> … para trabajar con tus archivos personales (JARVIS).", parse_mode="HTML")
        return
    recientes = sorted(_entrada().glob("*"), key=lambda p: p.stat().st_mtime)[-5:] if _entrada().is_dir() else []
    recientes = [p.name for p in recientes if time.time() - p.stat().st_mtime < 3600]
    await mandar_a_claude(ctx, update.effective_chat.id, texto, recientes)


# ---------- /prompt: DeepSeek redacta un buen prompt y Chava decide si se lo manda a Claude ----------

PROMPT_REDACTOR = """Eres experto en escribir prompts para Claude Code (un agente que trabaja en un servidor con acceso a archivos,
terminal, búsqueda web y skills para Word/PDF/PowerPoint/Excel). Convierte la idea de Salvador en un prompt claro y completo.

Contexto que Claude YA sabe (no lo repitas): las reglas de su carpeta escolar (CLAUDE.md: portada, nombre de archivo
26310453_<Título>, datos.md). Semestre activo: {sem}. Parcial actual: {parcial}. Fecha de hoy: {hoy}.
Carpetas de materias (usa el nombre EXACTO, con acentos y espacios): {mats}.
Ruta de una tarea: {sem}/<MATERIA EXACTA>/{parcial}/ (otro parcial solo si él lo dice).

El prompt debe incluir, si aplica: objetivo concreto; contexto; entregable exacto (formato, extensión, estructura,
nombre de archivo y carpeta SEMESTRE_N/MATERIA/Px); requisitos y criterios de calidad; qué NO hacer; y cómo verificar
el resultado. Si falta un dato importante, pon un supuesto razonable entre [corchetes] para que él lo cambie.
Escríbelo en español, directo, en segunda persona ("Haz…"), sin saludos ni explicaciones. Devuelve SOLO el prompt."""


async def generar_prompt(idea: str, previo: str = "") -> str:
    msgs = [{"role": "system", "content": PROMPT_REDACTOR.format(
                sem=semestre_actual(), mats=" | ".join(materias()), parcial=parcial_actual(),
                hoy=jarvis.ahora().strftime("%Y-%m-%d"))},
            {"role": "user", "content": idea}]
    if previo:
        msgs += [{"role": "assistant", "content": previo},
                 {"role": "user", "content": "Dame otra versión distinta y mejor (más específica)."}]
    r = await deepseek_chat("redactor", model=DEEPSEEK_MODEL, messages=msgs, temperature=0.7)
    return (r.choices[0].message.content or "").strip().strip("`")


def teclado_prompt(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Mandar a Claude", callback_data=f"pmt:{key}:go")],
        [InlineKeyboardButton("🔁 Otra versión", callback_data=f"pmt:{key}:otra"),
         InlineKeyboardButton("❌", callback_data=f"pmt:{key}:x")]])


async def mostrar_prompt(bot_, ctx: ContextTypes.DEFAULT_TYPE, chat: int, key: str, msg=None):
    p = ctx.bot_data["prompts"][key]["prompt"]
    texto = (f"📝 <b>Prompt para Claude</b> (toca el bloque para copiarlo y editarlo):\n\n<pre>{html.escape(p[:3500])}</pre>\n"
             "Si lo editas, mándalo con <code>/claude …</code>")
    if msg:
        await msg.edit_text(texto, parse_mode="HTML", reply_markup=teclado_prompt(key))
    else:
        await bot_.send_message(chat, texto, parse_mode="HTML", reply_markup=teclado_prompt(key))


async def cmd_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/prompt <idea> → prompt bien hecho, con botón para mandarlo a Claude. También respondiendo a un mensaje."""
    if not autorizado(update):
        return
    partes_txt = (update.message.text or "").split(maxsplit=1)
    idea = partes_txt[1].strip() if len(partes_txt) > 1 else ""
    citado = update.message.reply_to_message
    if citado and (citado.text or citado.caption):
        idea += f"\n\n(Referencia: {(citado.text or citado.caption)[:3000]})"
    if not idea.strip():
        await update.effective_chat.send_message(
            "Uso: <code>/prompt lo que quieres que haga Claude</code>\n"
            "Ej: <code>/prompt resumen del tema 2 de Discretas para estudiar, con ejemplos</code>\n"
            "Te lo redacto bien y con un botón se lo mando a Claude.", parse_mode="HTML")
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        p = await generar_prompt(idea)
    except Exception as e:
        await update.effective_chat.send_message(f"⚠️ No pude redactar el prompt: {e}")
        return
    key = uuid.uuid4().hex[:8]
    ctx.bot_data.setdefault("prompts", {})[key] = {"idea": idea, "prompt": p}
    await mostrar_prompt(ctx.bot, ctx, update.effective_chat.id, key)


async def mandar_a_claude(ctx: ContextTypes.DEFAULT_TYPE, chat: int, texto: str, recientes: list[str]):
    """Pedido directo a Claude (comando /claude, o /claude en el pie de una foto/archivo)."""
    if not texto.strip():
        texto = "Revisa estos archivos y dime qué contienen."
    personal = re.match(r"^(personal|p)\b[:\s]*", texto, re.I)
    if personal:
        texto = texto[personal.end():]
    tipo, materia = adivinar_pedido(texto)
    # Sin materia: las creaciones van a Documentos/Creaciones y lo demás a Escuela/OTROS (como antes).
    args = {"espacio": "personal" if personal or (not materia and tipo in CARPETA_TIPO) else "escuela",
            "materia": "" if personal else materia, "tipo": tipo, "titulo": texto[:40]}
    rel, visible, _ = destino_job(args)
    escuela = bool(materia) and not personal
    enviar_arch = not rel.startswith("JARVIS/")
    prompt = (f"Pedido directo de Salvador por Telegram (comando /claude):\n{texto}\n\n"
              f"Carpeta de trabajo: {visible} (la eligió KORRA: tipo {tipo}"
              f"{', materia ' + materia if materia else ''}). Ahí van los archivos FINALES."
              + (f"\nArchivos que mandó por Telegram (en /workspace/{ENTRADA_DIR}): {', '.join(recientes)}"
                 if recientes else "")
              + "\nTú NO tienes acceso a Google Calendar/Gmail: si pide agendar o mandar algo, termina tu respuesta "
                "con la lista EXACTA de lo que hay que agendar (título, fecha AAAA-MM-DD, hora si hay) y KORRA lo hace.")
    add_msg(chat, "user", f"[/claude] {texto}" + (f" (archivos: {', '.join(recientes)})" if recientes else ""))
    await lanzar_job(ctx, chat, prompt, rel, texto[:40] + ("…" if len(texto) > 40 else ""), enviar=enviar_arch,
                     tipo=tipo, escuela=escuela)


PALABRAS_TIPO = (("video", r"\bvideo|\bclip|\breel|\btiktok|animaci[oó]n|remotion|hyperframes"),
                 ("presentacion", r"presentaci[oó]n|diapositiva|\bppt|powerpoint|\bexpo(sici[oó]n)?\b|\bslides?\b"),
                 ("diseno", r"dise[ñn]o|flyer|invitaci[oó]n|p[oó]ster|banner|sticker|portada de|tarjeta|\blogo\b"),
                 ("tarea", r"\btarea|ensayo|resumen|investigaci[oó]n|reporte|\bword\b|cuestionario|mapa"))


# Palabras de nombres de materia que también salen en pedidos que no son de esa materia ("eventos de software").
GENERICAS = {"eventos", "introduccion", "orientada", "programacion", "matematicas"}


def adivinar_pedido(texto: str) -> tuple[str, str]:
    """(tipo, materia) de un pedido directo /claude, para elegir su carpeta sin preguntarle a DeepSeek."""
    t = norm(texto)
    tipo = next((tp for tp, rx in PALABRAS_TIPO if re.search(rx, texto, re.I)), "otro")
    materia = next((m for m in materias() if norm(m) in t or any(
        len(w) > 4 and w not in GENERICAS and w in re.findall(r"\w+", t) for w in norm(m).split())), "")
    return tipo, materia


# ---------- sesiones pasadas de Claude y skills globales ----------

SESION_ESPERA_S = 900  # tras elegir una sesión, cuánto espera tu mensaje antes de olvidarla
ES_SKILLS_ADD = re.compile(r"^(npx\s+(-y\s+)?)?skills(@\S+)?\s+(add|a)\s+\S", re.I)


async def pedir_texto_sesion(ctx: ContextTypes.DEFAULT_TYPE, chat: int, ses: dict):
    """Deja elegida una sesión: el siguiente mensaje de texto se le manda a Claude en ella."""
    ctx.chat_data["sesion"] = {"id": ses["id"], "cwd": ses["cwd"], "titulo": ses["titulo"], "ts": time.time()}
    await ctx.bot.send_message(
        chat, f"✍️ Escribe qué quieres que haga Claude en «{html.escape(ses['titulo'][:60])}».\n"
              f"Recuerda todo lo de esa sesión, no hace falta repetírselo.\n📁 {html.escape(ses['cwd'] or 'CETI COLOMOS')}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="ses:x")]]))


async def continuar_sesion(ctx: ContextTypes.DEFAULT_TYPE, chat: int, ses: dict, texto: str):
    add_msg(chat, "user", f"[/continuar «{ses['titulo']}»] {texto}")
    try:
        await lanzar_job(ctx, chat, texto, ses["cwd"], f"↪ {ses['titulo'][:38]}", resume=ses["id"])
    except LookupError:
        await ctx.bot.send_message(chat, "Esa sesión ya no existe. Mira las que hay con /sesiones.")


async def cmd_sesiones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sesiones → trabajos pasados de Claude, con botón para continuar cada uno."""
    if not autorizado(update):
        return
    lista = (await worker.get("/sesiones", params={"n": 8})).json()
    if not lista:
        await update.effective_chat.send_message("Todavía no hay sesiones de Claude.")
        return
    lineas = ["<b>🗂 Sesiones de Claude</b> (toca un número para continuarla)\n"]
    for i, x in enumerate(lista, 1):
        cuando = datetime.fromtimestamp(x["modificada"], jarvis.TZ).strftime("%d/%m %H:%M")
        lineas.append(f"<b>{i}.</b> {cuando} · {html.escape(x['titulo'][:70])}\n     📁 {html.escape(x['cwd'][-45:] or 'CETI COLOMOS')}")
    botones = [InlineKeyboardButton(str(i), callback_data=f"ses:{x['id']}") for i, x in enumerate(lista, 1)]
    await update.effective_chat.send_message(
        "\n".join(lineas), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([botones[i:i + 4] for i in range(0, len(botones), 4)]))


async def cmd_continuar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/continuar <qué hacer> → sigue la última sesión de Claude. Sin texto, te lo pregunta."""
    if not autorizado(update):
        return
    lista = (await worker.get("/sesiones", params={"n": 40})).json()
    ses = next((x for x in lista if x["id"] == kv_get("ultima_sesion")), lista[0] if lista else None)
    if not ses:
        await update.effective_chat.send_message("Todavía no hay sesiones de Claude.")
        return
    texto = " ".join(ctx.args or []).strip()
    if texto:
        await continuar_sesion(ctx, update.effective_chat.id, ses, texto)
    else:
        await pedir_texto_sesion(ctx, update.effective_chat.id, ses)


async def instalar_skills(chat, paquete: str):
    """Instala skills GLOBALES en la Pi (las ve este Claude y el de la terminal). No gasta límite de Claude."""
    aviso = await chat.send_message("⏳ Instalando skills globales…")
    try:
        r = await worker.post("/skills", json={"paquete": paquete}, timeout=330)
    except Exception as e:
        await aviso.edit_text(f"❌ No pude instalar: {e}")
        return
    if r.status_code != 200:
        await aviso.edit_text(f"❌ No pude instalar: {r.json().get('detail', r.status_code)}")
        return
    d = r.json()
    if d["ok"] and d["nuevas"]:
        texto = f"✅ Instalé {len(d['nuevas'])} skill(s) globales:\n" + ", ".join(d["nuevas"])
    elif d["ok"]:
        texto = "✅ Listo. No hay skills nuevas (ya estaban instaladas, se actualizaron)."
    else:
        texto = "❌ Falló la instalación:\n" + d["salida"][-700:]
    await aviso.edit_text(texto + f"\n\nTotal de skills globales: {d['total']}")


async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/skills → lista las skills globales · /skills <paquete o comando npx> → las instala."""
    if not autorizado(update):
        return
    paquete = " ".join(ctx.args or []).strip()
    if paquete:
        await instalar_skills(update.effective_chat, paquete)
        return
    d = (await worker.get("/skills")).json()
    texto = (f"<b>🧩 Skills globales de Claude ({len(d['skills'])})</b>\n{html.escape(', '.join(d['skills']))[:3300]}\n\n"
             f"<b>Plugins</b>: {html.escape(', '.join(d['plugins']) or 'ninguno')}\n\n"
             "Instalar: <code>/skills remotion-dev/skills</code> (o pega el comando <code>npx skills add …</code>)")
    await update.effective_chat.send_message(texto, parse_mode="HTML")


async def saldo_deepseek() -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.deepseek.com/user/balance",
                        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"})
    r.raise_for_status()
    return r.json()["balance_infos"][0]


async def job_estadisticas(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 15 min deja el resumen de uso que muestra la pestaña KORRA del homelab-dashboard."""
    try:
        claude = (await worker.get("/historial", timeout=60)).json()
    except Exception as e:
        log.warning("estadísticas: no pude leer el historial del worker (%s)", e)
        claude = []
    try:
        b = await saldo_deepseek()
        saldo = {"total": float(b["total_balance"]), "moneda": b["currency"]}
    except Exception as e:
        log.warning("estadísticas: no pude leer el saldo de DeepSeek (%s)", e)
        saldo = None
    try:
        resumen = estadisticas.generar(db, claude, saldo)  # en el hilo del bot: la conexión de SQLite es compartida
        await asyncio.to_thread(estadisticas.escribir, resumen)
    except Exception:
        log.exception("estadísticas")


async def cmd_uso(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    lineas = ["<b>📊 Uso</b>"]
    try:
        u = (await worker.get("/uso", timeout=40)).json()
        barra = lambda x: "▓" * round(float(x) * 10) + "░" * (10 - round(float(x) * 10))
        lineas.append(f"\n<b>Claude (plan Pro)</b>\nSesión 5 h: {barra(u.get('5h-utilization', 0))} "
                      f"{float(u.get('5h-utilization', 0)) * 100:.0f}% · se renueva {_hace(u.get('5h-reset'))}\n"
                      f"Semana: {barra(u.get('7d-utilization', 0))} {float(u.get('7d-utilization', 0)) * 100:.0f}% · "
                      f"se renueva {_hace(u.get('7d-reset'))}\n"
                      f"Estado: {'✅ disponible' if u.get('status') == 'allowed' else '⛔ límite alcanzado'} · "
                      f"créditos extra: {'no' if u.get('overage-status') == 'rejected' else u.get('overage-status', '?')}")
    except Exception as e:
        lineas.append(f"\nClaude: no pude leer el uso ({e})")
    try:
        b = await saldo_deepseek()
        lineas.append(f"\n<b>DeepSeek</b>\nSaldo: ${b['total_balance']} {b['currency']}")
    except Exception as e:
        lineas.append(f"\nDeepSeek: no pude leer el saldo ({e})")
    jobs = (await worker.get("/jobs")).json()
    hoy = [j for j in jobs if j.get("created", 0) > time.time() - 86400]
    lineas.append(f"\n<b>Trabajos de Claude (24 h)</b>: {len(hoy)}")
    for j in sorted(hoy, key=lambda j: j.get("created", 0)):
        hora = datetime.fromtimestamp(j.get("created", 0), jarvis.TZ).strftime("%H:%M")
        costo = f"${j['cost_usd']:.2f}" if j.get("cost_usd") is not None else j.get("status", "")
        lineas.append(f"• {hora} · {html.escape((j.get('cwd') or 'CETI COLOMOS')[-32:])} · {costo}")
    lineas.append("<i>(el $ es el costo equivalente: entre más alto, más límite se comió)</i>")
    await update.effective_chat.send_message("\n".join(lineas), parse_mode="HTML")


def _meminfo() -> dict[str, int]:
    """/proc/meminfo del servidor, en kB."""
    info = {}
    for linea in Path("/proc/meminfo").read_text().splitlines():
        k, v = linea.split(":", 1)
        info[k] = int(v.split()[0])
    return info


# ---------- limpieza de memoria ----------

HORA_LIMPIEZA = os.environ.get("HORA_LIMPIEZA", "04:30")  # limpieza automática diaria; "" la apaga
MAX_PENDIENTES = 30  # botones / trabajos que se conservan por tipo; lo más viejo se olvida
# bot_data crece con cada botón y cada trabajo de Claude (nunca se borraba nada)
PENDIENTES = ("opciones", "confirmar", "correos", "impresiones", "imprimibles")


def _rss_mb() -> float:
    for linea in Path("/proc/self/status").read_text().splitlines():
        if linea.startswith("VmRSS:"):
            return int(linea.split()[1]) / 1024
    return 0.0


async def limpiar_ram(app: Application) -> dict:
    """Olvida botones y trabajos viejos, le devuelve al sistema la RAM que Python ya no usa y le pide
    lo mismo al worker (que además mata procesos atorados). No reinicia nada ni toca la memoria de KORRA."""
    r = {"bot_antes": _rss_mb(), "olvidados": 0, "worker": None}
    for nombre in PENDIENTES:
        d = app.bot_data.get(nombre) or {}
        for k in list(d)[:-MAX_PENDIENTES]:  # los dict guardan el orden: los primeros son los más viejos
            del d[k]
            r["olvidados"] += 1
    invalidar_agenda()
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass
    r["bot_despues"] = _rss_mb()
    try:
        resp = await worker.post("/limpiar", timeout=30)
        resp.raise_for_status()
        r["worker"] = resp.json()
    except Exception as e:
        log.warning("limpiar worker: %s", e)
    return r


async def cmd_limpiar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    r = await limpiar_ram(ctx.application)
    lineas = ["<b>🧹 Limpieza de RAM</b>",
              f"KORRA (bot): {r['bot_antes']:.0f} → {r['bot_despues']:.0f} MB"]
    if w := r["worker"]:
        lineas.append(f"Worker (Claude): {w['antes_mb']:.0f} → {w['despues_mb']:.0f} MB")
        if w["ocupado"]:
            lineas.append("⏳ Claude está trabajando: no toqué sus procesos.")
        elif w["atorados"]:
            lineas.append(f"💀 Maté {w['atorados']} proceso(s) que se habían quedado atorados.")
        viejos = r["olvidados"] + w["jobs_borrados"]
    else:
        lineas.append("Worker (Claude): ❌ no contestó")
        viejos = r["olvidados"]
    lineas.append(f"Botones y trabajos viejos olvidados: {viejos}")
    info = _meminfo()
    lineas.append(f"\nServidor: {info['MemAvailable'] / 1048576:.1f} GB libres de {info['MemTotal'] / 1048576:.1f} · "
                  f"swap en uso {(info['SwapTotal'] - info['SwapFree']) / 1048576:.1f} GB")
    await update.effective_chat.send_message("\n".join(lineas), parse_mode="HTML")


async def job_limpieza(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = await limpiar_ram(ctx.application)
        log.info("Limpieza: bot %.0f → %.0f MB, %d olvidados, worker %s",
                 r["bot_antes"], r["bot_despues"], r["olvidados"], r["worker"])
    except Exception:
        log.exception("limpieza")


def remoto_linea() -> str:
    try:
        n = len(json.loads(remoto.ESTADO.read_text()).get("sesiones", []))
    except (OSError, ValueError):
        n = 0
    return (f"💻 Sesiones de Claude Code: {n} abierta(s)" if remoto.lanzador_vivo()
            else "⚠️ Lanzador de sesiones de Claude Code apagado")


async def cmd_servidor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    info = _meminfo()
    ram_total, ram_libre = info["MemTotal"] / 1048576, info["MemAvailable"] / 1048576
    disco = shutil.disk_usage(WORKSPACE)
    dias = float(Path("/proc/uptime").read_text().split()[0]) / 86400
    carga = Path("/proc/loadavg").read_text().split()[:3]
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except Exception:
        temp = None
    checks = []
    try:
        checks.append("✅ worker (Claude)" if (await worker.get("/health", timeout=5)).status_code == 200 else "❌ worker")
    except Exception:
        checks.append("❌ worker (Claude)")
    for c in g.conectadas():
        checks.append(f"{'⚠️' if g.permisos_faltantes_cache(c) else '✅'} Google {c}")
    texto = (f"<b>🖥 Servidor (Raspberry Pi 5)</b>\n"
             f"RAM: {ram_total - ram_libre:.1f} / {ram_total:.1f} GB · Disco: {disco.used / 1e9:.0f} / {disco.total / 1e9:.0f} GB\n"
             f"Carga: {' '.join(carga)}" + (f" · 🌡 {temp:.0f} °C" if temp else "") + f" · encendido {dias:.1f} días\n\n"
             + "\n".join(checks + [""] + estado_servidor.lineas_vigilante() + [remoto_linea()])
             + "\n\n/respaldos para ver el de iCloud · /pc para tus sesiones de Claude Code")
    await update.effective_chat.send_message(texto, parse_mode="HTML")


async def abrir_sesion_pc(materia: str = "", parcial: str = "", carpeta: str = "", nombre: str = "") -> dict:
    """Pide al lanzador del host una sesión de Claude Code (Remote Control) en la carpeta de una materia o en una ruta."""
    if materia:
        rel = carpeta_trabajo(materia, parcial or parcial_actual())  # ValueError si no existe la materia
        partes = rel.split("/")
        nombre = nombre or f"{partes[1].title()} {partes[2] if len(partes) > 2 else ''}".strip()
        destino = f"Documentos/Escuela/{rel}"
    else:
        destino = remoto.a_home(carpeta or "home")
    return await remoto.pedir("abrir", destino, nombre)


async def tool_sesion_claude(args: dict) -> str:
    accion = args.get("accion", "abrir")
    try:
        if accion == "abrir":
            r = await abrir_sesion_pc(args.get("materia", ""), args.get("parcial", ""), args.get("carpeta", ""),
                                      args.get("nombre", ""))
        else:
            r = await remoto.pedir(accion, nombre=args.get("nombre", ""))
    except ValueError as e:
        return str(e)
    if accion == "abrir" and r.get("ok"):
        return ("Sesión abierta. Dile EXACTAMENTE esto (con el link): " + remoto.texto_sesion(r)
                + "\nSe maneja desde la app; edita sola y pide permiso en el iPhone antes de correr comandos.")
    return json.dumps(r, ensure_ascii=False)


def teclado_sesiones(lista: list[dict]) -> InlineKeyboardMarkup | None:
    if not lista:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"🛑 Cerrar «{x['nombre'][:24]}»", callback_data=f"pc:x:{x['pid']}")]
                                 for x in lista])


async def cmd_pc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pc → lista (o abre una en el home si no hay) · /pc <materia [P2]> o /pc <ruta|atajo> → abre ahí."""
    if not autorizado(update):
        return
    chat = update.effective_chat
    texto = " ".join(ctx.args).strip()
    if not texto:
        r = await remoto.pedir("listar")
        if r.get("ok") and r.get("sesiones"):
            await chat.send_message("<b>💻 Sesiones de Claude Code abiertas</b>\n\n"
                                    + "\n\n".join(remoto.texto_sesion(x, nueva=False) for x in r["sesiones"])
                                    + "\n\n<code>/pc materia</code> o <code>/pc ruta</code> abre otra.",
                                    parse_mode="HTML", reply_markup=teclado_sesiones(r["sesiones"]),
                                    disable_web_page_preview=True)
            return
        if not r.get("ok"):
            await chat.send_message("⚠️ " + r.get("error", "No pude hablar con el lanzador."))
            return
    await chat.send_message("⏳ Abriendo Claude Code en la Pi…")
    m = re.match(r"^(.*?)\s*\b(p\s*[1-3])?$", texto, re.I)
    materia_txt, parcial = (m[1], (m[2] or "").replace(" ", "")) if m else (texto, "")
    try:
        if texto and buscar_materia(materia_txt):
            r = await abrir_sesion_pc(materia=materia_txt, parcial=parcial)
        else:
            r = await abrir_sesion_pc(carpeta=texto)
    except ValueError as e:
        r = {"ok": False, "error": str(e)}
    if r.get("ok"):
        await chat.send_message(remoto.texto_sesion(r, nueva=not r.get("ya_estaba"))
                                + ("\n(ya estaba abierta)" if r.get("ya_estaba") else
                                   "\nEdita sola y te pide permiso en el iPhone antes de correr comandos."),
                                parse_mode="HTML", disable_web_page_preview=True,
                                reply_markup=teclado_sesiones([r]))
    else:
        await chat.send_message("⚠️ " + r.get("error", "No se pudo abrir.")
                                + ("\n\nAbiertas: " + ", ".join(x["nombre"] for x in r["sesiones"]) if r.get("sesiones") else ""),
                                reply_markup=teclado_sesiones(r.get("sesiones", [])))


async def cmd_respaldos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await update.effective_chat.send_message(estado_servidor.texto_respaldos(), parse_mode="HTML")


async def _agenda_dias(update: Update, desde: datetime, dias: int, titulo: str):
    if not g.conectadas():
        await update.effective_chat.send_message("Conecta Google primero con /google.")
        return
    evs = await asyncio.to_thread(g.cal_listar, desde.strftime("%Y-%m-%d"), (desde + timedelta(days=dias - 1)).strftime("%Y-%m-%d"))
    nombres = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    por_dia: dict[str, list[str]] = {}
    for e in evs:
        hora = "todo el día" if e["todo_el_dia"] else e["inicio"][11:16]
        por_dia.setdefault(e["inicio"][:10], []).append(f"  {hora} — {html.escape(e['titulo'])}"
                                                        + (f" <i>({html.escape(e['lugar'][:40])})</i>" if e["lugar"] else ""))
    partes = [f"<b>{titulo}</b>"]
    for i in range(dias):
        d = desde + timedelta(days=i)
        clave = d.strftime("%Y-%m-%d")
        if dias == 1 or por_dia.get(clave):
            partes.append(f"\n<b>{nombres[d.weekday()]} {d:%d/%m}</b>\n" + ("\n".join(por_dia.get(clave, [])) or "  (libre 😎)"))
    await update.effective_chat.send_message("\n".join(partes)[:4000], parse_mode="HTML")


async def cmd_hoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await _agenda_dias(update, jarvis.ahora(), 1, "📅 Hoy")


async def cmd_manana(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await _agenda_dias(update, jarvis.ahora() + timedelta(days=1), 1, "📅 Mañana")


async def cmd_semana(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await _agenda_dias(update, jarvis.ahora(), 7, "📅 Próximos 7 días")


async def cmd_correos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    cs = await asyncio.to_thread(g.gmail_buscar, "is:unread newer_than:3d category:primary", "", 10)
    if not cs:
        await update.effective_chat.send_message("📭 Sin correos importantes sin leer (últimos 3 días).")
        return
    lineas = [f"• <b>{html.escape(re.sub(r'<.*?>', '', c['de']).strip()[:35])}</b> ({c['cuenta']})\n  {html.escape(c['asunto'][:80])}"
              for c in cs]
    await update.effective_chat.send_message(
        f"<b>📬 {len(cs)} sin leer (principal, 3 días)</b>\n\n" + "\n".join(lineas), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Resúmelos", callback_data="op_fijo:correos")]]))


# ---------- Empleos: vacantes de software y electrónica (como los eventos; lógica en empleos.py) ----------

HORA_EMPLEOS = os.environ.get("HORA_EMPLEOS", "02:00")
DIAS_EMPLEOS = os.environ.get("DIAS_EMPLEOS", "lu,ju")
HORA_AVISO_EMPLEOS = os.environ.get("HORA_AVISO_EMPLEOS", "08:20")
MODELO_EMPLEOS = os.environ.get("MODELO_EMPLEOS", "sonnet")  # verificar vacantes pide más juicio que los eventos
TRABAJO_EMPLEOS = {"model": MODELO_EMPLEOS, "max_turns": 70, "disallowed_tools": ["Task", "Agent", "Skill"]}


async def mandar_empleos(bot, vs: list[dict], titulo: str):
    if vs:
        await bot.send_message(ALLOWED_CHAT_ID, empleos.texto_lista(vs, titulo), parse_mode="HTML",
                               disable_web_page_preview=True, reply_markup=empleos.teclado(vs))


async def buscar_empleos(app: Application, avisar_sin_nuevos: bool = False, silencioso: bool = False):
    """`silencioso`: búsqueda de madrugada; las nuevas se avisan con job_aviso_empleos."""
    hoy = jarvis.ahora().strftime("%Y-%m-%d")
    empleos.BUSQUEDA_JSON.parent.mkdir(parents=True, exist_ok=True)
    empleos.BUSQUEDA_JSON.unlink(missing_ok=True)
    r = await worker.post("/jobs", json={"prompt": empleos.prompt_busqueda(hoy), "cwd": empleos.CWD,
                                         "timeout_s": 1800} | TRABAJO_EMPLEOS)
    job_id = r.json()["id"]
    while True:
        await asyncio.sleep(20)
        try:
            j = (await worker.get(f"/jobs/{job_id}")).json()
        except Exception:
            continue
        if j.get("status") not in ("queued", "running"):
            break
    leido = empleos.leer_busqueda()
    if leido is None:
        log.warning("búsqueda de empleos sin resultado (%s): %s", j.get("status"), (j.get("result") or "")[:200])
        if avisar_sin_nuevos:
            await app.bot.send_message(ALLOWED_CHAT_ID, "⚠️ La búsqueda de vacantes falló esta vez. Lo vuelvo a intentar en la siguiente vuelta.")
        return
    encontradas, contactos = leido
    nuevas = empleos.integrar(encontradas, hoy, avisado=not silencioso)
    n_contactos = empleos.integrar_contactos(contactos, hoy)
    log.info("Búsqueda de empleos: %d vacantes nuevas, %d contactos nuevos (costo equivalente $%s)",
             len(nuevas), n_contactos, j.get("cost_usd"))
    if silencioso:
        return
    if nuevas:
        await mandar_empleos(app.bot, nuevas[:10], f"💼 {len(nuevas)} vacante(s) nuevas para ti")
        add_msg(ALLOWED_CHAT_ID, "assistant", "[Le mandé vacantes nuevas: " +
                "; ".join(f"{v['puesto']} en {v['empresa']}" for v in nuevas[:10]) + "]")
    elif avisar_sin_nuevos:
        await app.bot.send_message(ALLOWED_CHAT_ID, "💼 Busqué y no salió nada nuevo. Lo que ya tengo está en /puestos.")


async def job_empleos(ctx: ContextTypes.DEFAULT_TYPE):
    """Búsqueda automática de madrugada, solo los días de DIAS_EMPLEOS y si no se ha gastado mucho límite."""
    if not claude_activo():
        return
    hoy = ["lu", "ma", "mi", "ju", "vi", "sa", "do"][jarvis.ahora().weekday()]
    if hoy not in [d.strip()[:2].lower() for d in DIAS_EMPLEOS.split(",")]:
        return
    try:
        uso = (await worker.get("/uso", timeout=40)).json()
        if float(uso.get("5h-utilization", 0)) > 0.5:
            log.info("Búsqueda de empleos saltada: ventana de Claude al %.0f%%", float(uso["5h-utilization"]) * 100)
            return
    except Exception:
        pass
    try:
        await buscar_empleos(ctx.application, silencioso=True)
    except Exception:
        log.exception("búsqueda de empleos")


async def job_aviso_empleos(ctx: ContextTypes.DEFAULT_TYPE):
    """En la mañana: manda las vacantes que encontró de madrugada."""
    vs = empleos.cargar()
    nuevas = empleos.ordenar([v for v in vs if v.get("avisado") is False])
    if not nuevas:
        return
    await mandar_empleos(ctx.bot, nuevas[:10], f"💼 {len(nuevas)} vacante(s) nuevas para ti")
    add_msg(ALLOWED_CHAT_ID, "assistant", "[Le mandé vacantes nuevas: " +
            "; ".join(f"{v['puesto']} en {v['empresa']}" for v in nuevas[:10]) + "]")
    for v in vs:
        if v.get("avisado") is False:
            v["avisado"] = True
    empleos.guardar(vs)


async def cmd_puestos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args and ctx.args[0].lower() in ("buscar", "ya", "nuevos", "nuevas"):
        if not claude_activo():
            await update.effective_chat.send_message(CLAUDE_APAGADO)
            return
        await update.effective_chat.send_message("🔎 Poniendo a Claude a buscarte puestos (software y electrónica)… tarda unos 10-15 min.")
        ctx.application.create_task(buscar_empleos(ctx.application, avisar_sin_nuevos=True))
        return
    texto, teclado, total = empleos.pagina_puestos(0)
    if not total:
        await update.effective_chat.send_message("Todavía no tengo puestos guardados. Manda <code>/puestos buscar</code>.",
                                                 parse_mode="HTML")
        return
    await update.effective_chat.send_message(texto, parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado)


async def job_recordar_aplicar(ctx: ContextTypes.DEFAULT_TYPE):
    """Una sola vez por puesto: si su CV a la medida quedó listo hace más de 24 h y no ha dicho nada, le pregunta."""
    for v in empleos.para_recordar():
        await ctx.bot.send_message(
            ALLOWED_CHAT_ID, f"🤔 ¿Ya aplicaste a <b>{html.escape(v['puesto'])}</b> — {html.escape(v['empresa'])}? "
                             f"Tu CV a la medida ya está listo desde ayer.\n<a href=\"{html.escape(v.get('link', ''), quote=True)}\">abrir la vacante</a>",
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ya apliqué", callback_data=f"emp:apl:{v['id']}"),
                                                InlineKeyboardButton("🙅 Ya no me interesa", callback_data=f"emp:no:{v['id']}")]]))
        empleos.poner_estado(v["id"], v["estado"], recordado=True)


# ---------- Correos de empleo: lote diario con un botón (lógica en correos_empleo.py) ----------

CUENTA_EMPLEO = os.environ.get("CUENTA_EMPLEO", "empleo")
HORA_LOTE_CORREOS = os.environ.get("HORA_LOTE_CORREOS", "09:30")
DIAS_LOTE_CORREOS = os.environ.get("DIAS_LOTE_CORREOS", "lu,ma,mi,ju,vi")
MAX_CORREOS_DIA = int(os.environ.get("MAX_CORREOS_DIA", "25"))
TRABAJO_CORREOS = {"model": os.environ.get("MODELO_CORREOS", "sonnet"), "max_turns": 30,
                   "disallowed_tools": ["Task", "Agent", "Skill", "Bash"]}


def empleo_conectado() -> bool:
    return g.real(CUENTA_EMPLEO) in g.conectadas()


def teclado_lote(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Enviar los {n}", callback_data="lote:enviar")],
                                 [InlineKeyboardButton("👀 Ver / quitar", callback_data="lote:ver"),
                                  InlineKeyboardButton("❌ Hoy no", callback_data="lote:no")]])


def texto_lote(d: dict) -> str:
    filas = [f"{i}. <b>{html.escape(x['empresa'] or x['correo'])}</b> {'💻' if x.get('perfil') == 'software' else '🔧'}"
             f"{' · seguimiento' if x['tipo'] == 'seguimiento' else ''}\n   <i>{html.escape(x['asunto'])}</i>"
             for i, x in enumerate(d["items"], 1)]
    return (f"📨 <b>Correos de empleo de hoy ({len(d['items'])})</b>\nCada uno personalizado, con tu CV adjunto, desde tu "
            f"Gmail de empleo.\n\n" + "\n".join(filas))[:4000]


async def armar_lote_correos(bot_tg, manual: bool = False):
    """Redacta el lote del día con Claude y se lo manda a Chava para que lo apruebe con un botón."""
    async def decir(t):
        if manual:
            await bot_tg.send_message(ALLOWED_CHAT_ID, t)
    if not empleo_conectado():
        await decir(f"📭 Primero conecta tu Gmail de empleo: /google {CUENTA_EMPLEO}")
        return
    if ce.lote_actual():
        await decir("Ya hay un lote esperando tu botón: /lote para verlo.")
        return
    hoy = jarvis.ahora().date()
    lote = ce.seleccionar(hoy, ce.limite_hoy(hoy, MAX_CORREOS_DIA))
    if not lote:
        await decir("Hoy no hay a quién escribirle (o ya llegaste al límite del día). Los contactos salen de /puestos buscar.")
        return
    ce.BORRADORES.unlink(missing_ok=True)
    r = await worker.post("/jobs", json={"prompt": ce.prompt_borradores(lote), "cwd": ce.CWD} | TRABAJO_CORREOS)
    job_id = r.json()["id"]
    while True:
        await asyncio.sleep(15)
        try:
            j = (await worker.get(f"/jobs/{job_id}")).json()
        except Exception:
            continue
        if j.get("status") not in ("queued", "running"):
            break
    d = ce.armar_lote(lote, hoy)
    if not d:
        log.warning("lote de correos sin borradores (%s): %s", j.get("status"), (j.get("result") or "")[:200])
        await decir("⚠️ No pude redactar los correos esta vez.")
        return
    await bot_tg.send_message(ALLOWED_CHAT_ID, texto_lote(d), parse_mode="HTML", reply_markup=teclado_lote(len(d["items"])))


async def enviar_lote_correos(bot_tg):
    if kv_get("lote_enviando") == "1":
        return
    d = ce.lote_actual()
    if not d:
        await bot_tg.send_message(ALLOWED_CHAT_ID, "Ese lote ya no está.")
        return
    kv_set("lote_enviando", "1")
    enviados, pendientes = 0, list(d["items"])
    try:
        while pendientes:
            x = pendientes[0]
            cv = ce.adjunto(x.get("perfil") or "software")
            if not cv.exists():
                await bot_tg.send_message(ALLOWED_CHAT_ID, f"⚠️ No encuentro tu CV ({cv.name}); paré el envío.")
                break
            gid = await asyncio.to_thread(g.gmail_enviar, CUENTA_EMPLEO, x["correo"], x["asunto"], x["cuerpo"], "", [cv])
            ce.registrar_envio(x, gid, jarvis.ahora().isoformat(timespec="seconds"))
            enviados += 1
            pendientes.pop(0)
            empleos._escribir(ce.LOTE, d | {"items": pendientes})
            if pendientes:
                await asyncio.sleep(60 + uuid.uuid4().int % 90)  # 1-2.5 min entre correos
    except Exception as e:
        log.exception("envío del lote de empleo")
        await bot_tg.send_message(ALLOWED_CHAT_ID, f"⚠️ Paré el envío después de {enviados}: {e}\nLos que faltan siguen en /lote.")
        return
    finally:
        kv_set("lote_enviando", "0")
    if not pendientes:
        ce.descartar_lote()
    await bot_tg.send_message(ALLOWED_CHAT_ID, f"✅ Mandé {enviados} correo(s) de empleo. Te aviso cuando alguien conteste.")
    add_msg(ALLOWED_CHAT_ID, "assistant", f"[Mandé {enviados} correos de empleo con su CV]")


async def job_lote_correos(ctx: ContextTypes.DEFAULT_TYPE):
    hoy = ["lu", "ma", "mi", "ju", "vi", "sa", "do"][jarvis.ahora().weekday()]
    if hoy not in [d.strip()[:2].lower() for d in DIAS_LOTE_CORREOS.split(",")] or not claude_activo():
        return
    try:
        await armar_lote_correos(ctx.bot)
    except Exception:
        log.exception("lote de correos de empleo")


async def job_respuestas_empleo(ctx: ContextTypes.DEFAULT_TYPE):
    """Revisa la bandeja de la cuenta de empleo: respuestas, "no gracias" y rebotes."""
    if not empleo_conectado():
        return
    try:
        nuevos = await asyncio.to_thread(g.gmail_buscar, "in:inbox -category:promotions -category:social newer_than:30d -from:me", CUENTA_EMPLEO, 30)
    except Exception:
        log.exception("bandeja de empleo")
        return
    if not ce.CUENTA_JSON.exists():
        try:
            perfil = await asyncio.to_thread(lambda: g._svc(CUENTA_EMPLEO, "gmail", "v1").users().getProfile(userId="me").execute())
            ce.guardar_cuenta(perfil.get("emailAddress", ""))
        except Exception:
            log.warning("no pude leer la dirección del Gmail de empleo")
    vistos = set(json.loads(kv_get("empleo_vistos", "[]")))
    msgs = []
    for m in nuevos:
        if m["id"] in vistos:
            continue
        try:
            completo = await asyncio.to_thread(g.gmail_leer, CUENTA_EMPLEO, m["id"])
        except Exception:
            continue
        msgs.append({"de": m.get("de", ""), "asunto": m.get("asunto", ""), "cuerpo": completo.get("cuerpo", "")[:3000]})
        vistos.add(m["id"])
    kv_set("empleo_vistos", json.dumps(sorted(vistos)[-500:]))
    avisos = ce.procesar_respuestas(msgs, jarvis.ahora().isoformat(timespec="seconds"))
    ce.guardar_bandeja(nuevos)  # despues de procesar: asi ya sabe de que empresa es cada uno
    if avisos:
        e = html.escape
        lineas = [("📬 Te contestó " if a["tipo"] == "respondio" else "🙅 Dijo que no / no escribir: " if a["tipo"] == "no_contactar"
                   else "↩️ Rebotó (correo malo): ") + f"<b>{e(a['empresa'])}</b> ({e(a['correo'])})"
                  + (f"\n   <i>{e(a.get('asunto', ''))}</i>" if a.get("asunto") else "") for a in avisos]
        await ctx.bot.send_message(ALLOWED_CHAT_ID, "💼 <b>Tu correo de empleo</b>\n" + "\n".join(lineas), parse_mode="HTML")
        add_msg(ALLOWED_CHAT_ID, "assistant", "[Le avisé respuestas de su correo de empleo: " +
                "; ".join(f"{a['tipo']} {a['empresa']}" for a in avisos) + "]")


async def iniciar_cv(ctx: ContextTypes.DEFAULT_TYPE, chat: int, vid: str, desde_app: bool = False) -> dict | None:
    """⭐ Me interesa (Telegram o app): marca la vacante, le manda el link para aplicar con [✅ Ya apliqué] y lanza su
    CV a la medida (el PDF le llega solo al terminar). None si la vacante ya no existe."""
    v = empleos.pedir_cv(vid)
    if not v:
        return None
    perfil = "electrónica" if empleos.carpeta_cv(v) == "Electronica" else "software"
    donde = ("Te va a aparecer en tu app (Jobs → Listos para aplicar): ahí nada más le das «Sí, ya apliqué»."
             if desde_app else "Cuando apliques, márcalo aquí o en tu app (Jobs) 👇")
    await ctx.bot.send_message(
        chat, f"⭐ <b>{html.escape(v['puesto'])}</b> — {html.escape(v['empresa'])}\n"
              f"Te armo un CV a la medida (perfil {perfil}); te llega aquí en 2-3 min.\n\n"
              f"🔗 Para aplicar: <a href=\"{html.escape(v.get('link', ''), quote=True)}\">abrir la vacante</a>\n{donde}",
        parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ya apliqué", callback_data=f"emp:apl:{vid}"),
                                            InlineKeyboardButton("🙅 Ya no me interesa", callback_data=f"emp:no:{vid}")]]))
    await lanzar_job(ctx, chat, empleos.prompt_cv(v), empleos.CWD, f"CV para {v['puesto']} en {v['empresa']}", tipo="documento")
    add_msg(chat, "user", f"[Me interesa {v['puesto']} en {v['empresa']}{' (desde la app)' if desde_app else ''}: "
                          "le pedí a Claude su CV a la medida]")
    return v


async def datos_vacante(texto: str) -> dict:
    """Puesto, empresa, link… de la vacante que pegó con /cv (DeepSeek, JSON). {} si no se pudo."""
    try:
        resp = await deepseek_chat(
            "cv_vacante", model=DEEPSEEK_MODEL, response_format={"type": "json_object"}, temperature=0.1,
            messages=[{"role": "system", "content": (
                "Extrae de esta vacante un JSON con: puesto (corto, como lo anuncian), empresa (nombre comercial "
                "corto, SIN 'S.A. de C.V.' ni similares; '' si no viene), link (URL o ''), perfil ('electronica' si es "
                "de producción, pruebas, manufactura, técnico electrónico; si no 'software'), zona, modalidad "
                "(Presencial/Híbrido/Remoto o ''), sueldo (o ''), ingles ('no', 'lectura', 'conversacion' o 'fluido'). "
                "No inventes: lo que no venga va vacío.")},
                {"role": "user", "content": texto[:6000]}])
        d = json.loads(resp.choices[0].message.content or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        log.exception("datos de la vacante de /cv")
        return {}


async def job_pedidos_cv(ctx: ContextTypes.DEFAULT_TYPE):
    """La app (Jobs → ⭐ Me interesa) deja un pedido: KORRA arma el CV a la medida y se lo manda por Telegram."""
    for ruta, pedido in empleos.pedidos_cv():
        ruta.unlink(missing_ok=True)
        vid = str(pedido.get("id", ""))
        if not claude_activo():
            empleos.poner_estado(vid, "me_interesa", cv_solicitado=None)  # para que pueda volver a darle ⭐ en la app
            await ctx.bot.send_message(ALLOWED_CHAT_ID, "⭐ Le diste «Me interesa» en la app, pero " + CLAUDE_APAGADO[2:]
                                       + " Préndelo y vuelve a darle ⭐.")
            continue
        try:
            await iniciar_cv(ctx, ALLOWED_CHAT_ID, vid, desde_app=True)
        except Exception as ex:
            log.exception("cv desde la app")
            empleos.poner_estado(vid, "me_interesa", cv_solicitado=None)
            await ctx.bot.send_message(ALLOWED_CHAT_ID, f"⚠️ No pude armar el CV que pediste en la app: {ex}")


async def job_pedidos_empleo(ctx: ContextTypes.DEFAULT_TYPE):
    """La app (Jobs → Correos → 🔔) deja un pedido: KORRA le manda ese correo por Telegram con botones."""
    for ruta, pedido in ce.pedidos_pendientes():
        ruta.unlink(missing_ok=True)
        mid = str(pedido.get("id", ""))
        try:
            m = await asyncio.to_thread(g.gmail_leer, CUENTA_EMPLEO, mid)
        except Exception as ex:
            await ctx.bot.send_message(ALLOWED_CHAT_ID, f"⚠️ No pude abrir ese correo de tu Gmail de empleo: {ex}")
            continue
        e = html.escape
        empresa = ce.empresa_de(ce._remitente(m.get("de", "")))
        texto = (f"📬 <b>{e(m.get('asunto') or '(sin asunto)')}</b>\n"
                 f"De: {e(m.get('de', ''))}{f' · {e(empresa)}' if empresa else ''}\n<i>{e(m.get('fecha', ''))}</i>\n\n"
                 f"{e((m.get('cuerpo') or '').strip()[:2800])}")
        botones = [[InlineKeyboardButton("✍️ Ayúdame a contestar", callback_data=f"mail:resp:{mid}")]]
        if url := ce.url_gmail(m.get("hilo", "")):
            botones.append([InlineKeyboardButton("✉️ Abrir en Gmail", url=url)])
        await ctx.bot.send_message(ALLOWED_CHAT_ID, texto[:4000], parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=InlineKeyboardMarkup(botones))
        add_msg(ALLOWED_CHAT_ID, "assistant", f"[Le mandé el correo de empleo «{m.get('asunto', '')}» de {m.get('de', '')} (id {mid})]")


async def cmd_lote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args and ctx.args[0].lower() in ("ya", "armar", "nuevo"):
        if not claude_activo():
            await update.effective_chat.send_message(CLAUDE_APAGADO)
            return
        await update.effective_chat.send_message("✍️ Redactando los correos de hoy… tarda unos minutos.")
        ctx.application.create_task(armar_lote_correos(ctx.bot, manual=True))
        return
    d = ce.lote_actual()
    if d:
        await update.effective_chat.send_message(texto_lote(d), parse_mode="HTML", reply_markup=teclado_lote(len(d["items"])))
        return
    r = ce.resumen()
    hoy = jarvis.ahora().date()
    await update.effective_chat.send_message(
        f"📨 Sin lote pendiente. Contactos: {r['pendiente']} por escribir · {r['enviado']} esperando respuesta · "
        f"{r['respondio']} contestaron · {r['no_contactar']} no escribir · {r['rebote']} rebotes. "
        f"Hoy puedes mandar hasta {ce.limite_hoy(hoy, MAX_CORREOS_DIA)}."
        + ("" if empleo_conectado() else f"\n\n📭 Falta conectar tu Gmail de empleo: /google {CUENTA_EMPLEO}")
        + "\nPara armar uno ahorita: /lote ya")


async def cmd_cv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/cv → sus CVs con botón para recibirlos · /cv <detalles del puesto> → CV a la medida (REGLAS_CV.md)."""
    if not autorizado(update):
        return
    texto = (update.message.text or "").split(maxsplit=1)
    texto = texto[1].strip() if len(texto) > 1 else ""
    if len(texto) >= 15:
        if not claude_activo():
            await update.effective_chat.send_message(CLAUDE_APAGADO)
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        v = empleos.vacante_de_cv(await datos_vacante(texto), texto)
        await iniciar_cv(ctx, update.effective_chat.id, v["id"])  # igual que ⭐: link + [✅ Ya apliqué] y sale en la app
        return
    cvs = empleos.lista_cvs()
    if not cvs:
        await update.effective_chat.send_message("Todavía no tengo tus CVs en Empleo/CVs.")
        return
    kv_set("cv_lista", json.dumps([str(f) for f in cvs]))
    botones = [[InlineKeyboardButton(("⭐ " if "00_Maestros" in str(f) else "📄 ") +
                                     f.stem.replace("Salvador_Pantoja_CV_", "").replace("_", " ")[:40],
                                     callback_data=f"cv:{i}")] for i, f in enumerate(cvs)]
    await update.effective_chat.send_message(
        "📄 <b>Tus CVs</b> (⭐ = los generales de cada perfil). Pícale a uno y te lo mando en PDF.\n\n"
        "Para uno a la medida: <code>/cv</code> + pega los detalles del puesto (o el link y lo que piden).",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(botones))


async def cmd_eventos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args and ctx.args[0].lower() in ("buscar", "ya", "nuevos") and not claude_activo():
        await update.effective_chat.send_message(CLAUDE_APAGADO)
        return
    if ctx.args and ctx.args[0].lower() in ("buscar", "ya", "nuevos"):
        await update.effective_chat.send_message("🔎 Poniendo a Claude a buscar eventos… tarda unos minutos.")
        ctx.application.create_task(buscar_eventos(ctx.application, avisar_sin_nuevos=True))
        return
    evs = eventos_proximos(30)
    if not evs:
        await update.effective_chat.send_message("No tengo eventos guardados todavía. Manda <code>/eventos buscar</code>.",
                                                 parse_mode="HTML")
        return
    await mandar_eventos(ctx.bot, evs[:10], "🗓 Eventos de software en GDL (próximos 30 días)")


# ---------- Iniciativa propia: KORRA te busca cuando hay un motivo real ----------

NIVELES = {"off": (0, 0), "baja": (1, 4.0), "media": (3, 2.5), "alta": (5, 1.5)}  # (máx. al día, horas mín. entre mensajes)
HORARIO_TRABAJO = os.environ.get("HORARIO_TRABAJO", "lu-sa 06:00-14:00")
HORAS_DORMIR = os.environ.get("HORAS_DORMIR", "22:00-07:00")
DIAS_IDX = {"lu": 0, "ma": 1, "mi": 2, "ju": 3, "vi": 4, "sa": 5, "do": 6}


def nivel_iniciativa() -> str:
    return kv_get("iniciativa_nivel", os.environ.get("INICIATIVA", "media"))


def _en_rango(hhmm: str, rango: str) -> bool:
    ini, fin = rango.split("-")
    return ini <= hhmm < fin if ini <= fin else (hhmm >= ini or hhmm < fin)  # rangos que cruzan medianoche


def ocupado_ahora(now: datetime) -> str:
    """'' si está libre; si no, el motivo (dormido, trabajo, clase, silencio)."""
    hhmm = now.strftime("%H:%M")
    if float(kv_get("silencio_hasta", "0") or 0) > time.time():
        return "silencio"
    if _en_rango(hhmm, HORAS_DORMIR):
        return "dormido"
    dias, _, horas = HORARIO_TRABAJO.partition(" ")
    d0, _, d1 = dias.partition("-")
    if DIAS_IDX.get(d0, 0) <= now.weekday() <= DIAS_IDX.get(d1 or d0, 5) and _en_rango(hhmm, horas):
        return "trabajo"
    try:
        for e in g.cal_listar(now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")):
            if e["calendario"] == g.CAL_CLASES[0] and e["inicio"][11:] <= hhmm < e["fin"][11:]:
                return "clase"
    except Exception:
        pass
    return ""


def _contador_hoy() -> int:
    d = json.loads(kv_get("proactivos", "{}") or "{}")
    return d.get("n", 0) if d.get("fecha") == jarvis.ahora().strftime("%Y-%m-%d") else 0


def _sumar_contador():
    kv_set("proactivos", json.dumps({"fecha": jarvis.ahora().strftime("%Y-%m-%d"), "n": _contador_hoy() + 1}))


def _ya_tocado(clave: str) -> bool:
    return clave in json.loads(kv_get("motivos_tocados", "[]") or "[]")


def _marcar_tocado(clave: str):
    lista = json.loads(kv_get("motivos_tocados", "[]") or "[]")[-300:]
    kv_set("motivos_tocados", json.dumps(lista + [clave]))


def motivos_iniciativa(now: datetime) -> list[tuple[str, str]]:
    """(clave, instrucción) de las razones para escribirle, en orden de prioridad."""
    hoy = now.strftime("%Y-%m-%d")
    m: list[tuple[str, str]] = []
    # 1) seguimientos que ya toca preguntar
    for r in memoria.db.execute(
            "SELECT id, texto FROM recuerdos WHERE categoria='seguimiento' AND estado='activo' "
            "AND json_extract(extra, '$.preguntar') <= ?", (now.strftime("%Y-%m-%d %H:%M"),)):
        m.append((f"seg-{r[0]}", f"Quedaste de darle seguimiento a esto (recuerdo #{r[0]}): «{r[1]}». Pregúntale cómo le fue / "
                                 "en qué quedó, de forma natural. Con lo que conteste, actualiza tu memoria."))
    # 1b) pendientes que vencen hoy o mañana (entregas, trámites): que no se le pasen
    for r in memoria.db.execute(
            "SELECT id, texto, vigente_hasta FROM recuerdos WHERE categoria='pendiente' AND estado='activo' "
            "AND vigente_hasta BETWEEN ? AND ?", (hoy, (now + timedelta(days=1)).strftime("%Y-%m-%d"))):
        if not _ya_tocado(f"pend-{r[0]}"):
            m.append((f"pend-{r[0]}", f"Tiene este pendiente que vence el {r[2]} (recuerdo #{r[0]}): «{r[1]}». Pregúntale "
                                      "si ya quedó o si le ayudas con algo; si ya quedó, olvídalo con `olvidar`."))
    # 2) exámenes en los próximos 3 días
    try:
        for e in g.cal_listar(hoy, (now + timedelta(days=3)).strftime("%Y-%m-%d")):
            if e["calendario"] == g.CAL_CETI_EVALUACIONES[0] and "xamen" in e["titulo"] and not _ya_tocado(f"exa-{e['id']}"):
                m.append((f"exa-{e['id']}", f"Tiene «{e['titulo']}» el {e['inicio']}. Pregúntale cómo va con el estudio y "
                                             "ofrécele (con botones) que Claude le arme una guía de estudio o preguntas de práctica."))
    except Exception:
        pass
    # 3) eventos de software de esta semana que no ha agendado
    for e in eventos_proximos(5):
        if e.get("estado") == "nuevo" and not _ya_tocado(f"evt-{e['id']}"):
            m.append((f"evt-{e['id']}", f"Esta semana hay «{e['nombre']}» ({e['inicio']}, {e.get('lugar', '')}, {e.get('costo', '')}). "
                                         "Pregúntale si le late ir; si sí, que le pique a /eventos para agendarlo."))
            break
    # 4) cómo amaneció el ánimo (diario de ayer)
    ayer = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    p = jarvis.DIARIO / f"{ayer}.md"
    if p.exists() and re.search(r"cansad|estresad|agotad|triste|preocupad|desvel|enferm|frustrad|agobiad", p.read_text(encoding="utf-8"), re.I) \
            and not _ya_tocado(f"animo-{ayer}"):
        m.append((f"animo-{ayer}", "En tu diario de ayer lo viste cansado/estresado. Pregúntale cómo sigue, con cariño y sin sermón."))
    # 5) lleva mucho sin escribir
    ultimo = db.execute("SELECT max(ts) FROM msgs WHERE chat=? AND role='user' AND content NOT LIKE '[%'",
                        (ALLOWED_CHAT_ID,)).fetchone()[0] or 0
    if time.time() - ultimo > 20 * 3600 and not _ya_tocado(f"silencio-{hoy}"):
        m.append((f"silencio-{hoy}", "Lleva casi un día sin escribirte. Búscalo como compa: algo concreto de su día o de lo que "
                                     "tiene pendiente, no un '¿qué onda?' genérico."))
    # 6) curiosidad: conocerlo mejor (máx. 1 al día)
    if not _ya_tocado(f"curiosidad-{hoy}"):
        conteo = memoria.conteo()
        cat = min(("familia", "salud", "proyectos", "gustos", "trabajo"), key=lambda c: conteo.get(c, 0))
        m.append((f"curiosidad-{hoy}", f"Quieres conocerlo mejor: sabes muy poco de «{memoria.CATEGORIAS.get(cat, cat)}» "
                                       f"({conteo.get(cat, 0)} datos). Hazle UNA pregunta ligera y natural sobre eso (si queda, con "
                                       "botones de respuesta rápida). Nada de interrogatorios."))
    return m


async def hablar_por_iniciativa(ctx: ContextTypes.DEFAULT_TYPE, instruccion: str) -> bool:
    """Turno propio de KORRA CON herramientas (puede ver el calendario, poner botones, guardar memoria).
    Devuelve False si decidió no escribir (NADA)."""
    from types import SimpleNamespace
    chat = ALLOWED_CHAT_ID
    upd = SimpleNamespace(effective_chat=_ChatFalso(ctx.bot, chat))
    add_msg(chat, "user", f"[SISTEMA — Chava no escribió esto. {instruccion} Mensaje corto (1-3 líneas), con tu "
                          "personalidad, que invite a contestar. Si de verdad no tiene sentido escribirle ahora, responde "
                          "exactamente: NADA]")
    try:
        texto, opciones, _ = await llamar_deepseek(upd, ctx, chat)
    except Exception:
        log.exception("iniciativa")
        texto, opciones = "", []
    if not texto or texto.strip(" .!¡\"'*").upper().startswith("NADA"):
        db.execute("DELETE FROM msgs WHERE id=(SELECT max(id) FROM msgs WHERE chat=? AND role='user')", (chat,))
        db.commit()
        return False
    add_msg(chat, "assistant", texto)
    await enviar(ctx.bot, chat, texto, teclado_opciones(ctx, opciones) if opciones else None)
    return True


async def job_pulso(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 30 min: ¿hay un motivo real para escribirle, está libre y no se ha pasado del límite?"""
    try:
        maximo, horas_min = NIVELES.get(nivel_iniciativa(), NIVELES["media"])
        if maximo == 0 or _contador_hoy() >= maximo:
            return
        now = jarvis.ahora()
        ultimo = db.execute("SELECT max(ts) FROM msgs WHERE chat=?", (ALLOWED_CHAT_ID,)).fetchone()[0] or 0
        if time.time() - ultimo < horas_min * 3600:
            return  # acaban de platicar o ya le escribió hace poco
        if motivo := await asyncio.to_thread(ocupado_ahora, now):
            return
        motivos = await asyncio.to_thread(motivos_iniciativa, now)
        if not motivos:
            return
        clave, instruccion = motivos[0]
        if await hablar_por_iniciativa(ctx, instruccion):
            _sumar_contador()
            log.info("Iniciativa: %s", clave)
        _marcar_tocado(clave)  # aunque decida NADA, no se insiste con lo mismo
        if clave.startswith("seg-"):
            memoria.db.execute("UPDATE recuerdos SET estado='vencido' WHERE id=?", (int(clave[4:]),))
            memoria.db.commit()
            memoria.exportar_md()
    except Exception:
        log.exception("pulso de iniciativa")


def programar_seguimiento(texto: str, cuando: str) -> str:
    """Algo que KORRA le va a PREGUNTAR más adelante (≠ recordatorio, que es avisarle)."""
    cuando = cuando.strip().replace("T", " ")
    if len(cuando) == 10:
        cuando += " 15:00"
    datetime.strptime(cuando[:16], "%Y-%m-%d %H:%M")  # valida el formato
    vence = (datetime.fromisoformat(cuando[:10]) + timedelta(days=4)).strftime("%Y-%m-%d")
    cur = memoria.db.execute(
        "INSERT INTO recuerdos(categoria, texto, importancia, vigente_hasta, estado, fuente, extra, creado, actualizado) "
        "VALUES ('seguimiento', ?, 2, ?, 'activo', 'chat', ?, ?, ?)",
        (texto.strip(), vence, json.dumps({"preguntar": cuando[:16]}), time.time(), time.time()))
    memoria.db.commit()
    memoria.exportar_md()
    return f"Seguimiento #{cur.lastrowid}: le preguntarás a partir de {cuando[:16]} (cuando esté libre)."


INICIATIVA_TOOLS = [{"type": "function", "function": {
    "name": "seguimiento",
    "description": ("Programa que TÚ le preguntes algo a Chava más adelante: '¿cómo te fue en el examen?', '¿qué te dijeron en "
                    "la entrevista?', '¿ya te sientes mejor?'. Úsala SOLA cuando te cuente algo que va a pasar. "
                    "(Distinto de recordatorio_crear, que es avisarle algo a una hora exacta.)"),
    "parameters": {"type": "object", "properties": {
        "texto": {"type": "string", "description": "Qué preguntarle y por qué, p. ej. 'Preguntar cómo le fue en el examen del 2º parcial de Economía (21-oct)'."},
        "cuando": {"type": "string", "description": "Desde cuándo preguntar: 'AAAA-MM-DD HH:MM' (normalmente un poco después de que pase)."}},
        "required": ["texto", "cuando"]}}}]


async def cmd_iniciativa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args and ctx.args[0].lower() in NIVELES:
        kv_set("iniciativa_nivel", ctx.args[0].lower())
    n = nivel_iniciativa()
    maximo, horas = NIVELES[n]
    silencio = float(kv_get("silencio_hasta", "0") or 0)
    await update.effective_chat.send_message(
        f"🧭 Iniciativa: <b>{n}</b> — hasta {maximo} mensajes extra al día, mínimo {horas:g} h entre uno y otro.\n"
        f"Hoy van {_contador_hoy()}. No te escribo dormido ({HORAS_DORMIR}), en el jale ({HORARIO_TRABAJO}) ni en clase."
        + (f"\n🤫 En silencio hasta {datetime.fromtimestamp(silencio, jarvis.TZ):%H:%M}." if silencio > time.time() else ""),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(("✅ " if k == n else "") + k, callback_data=f"ini:{k}") for k in NIVELES]]))


async def cmd_silencio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    try:
        horas = float(ctx.args[0]) if ctx.args else 2
    except ValueError:
        horas = 2
    if horas <= 0:
        kv_set("silencio_hasta", "0")
        await update.effective_chat.send_message("🔊 Listo, ya puedo volver a escribirte.")
        return
    kv_set("silencio_hasta", str(time.time() + horas * 3600))
    await update.effective_chat.send_message(
        f"🤫 Va, no te escribo por iniciativa propia en {horas:g} h (recordatorios y entregas sí). "
        "<code>/silencio 0</code> para quitarlo.", parse_mode="HTML")


async def job_memoria(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 5 min: aplica lo que Chava haya editado en memoria.md y caduca lo vencido."""
    try:
        if r := memoria.sincronizar_md():
            log.info("memoria.md: %s", r)
        memoria.caducar()
    except Exception:
        log.exception("sincronizar memoria")


async def post_init(app: Application):
    jarvis.init(NOMBRE)
    memoria.init(db)
    await retomar_jobs(app)
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMANDOS])  # menú al escribir "/"
    jq = app.job_queue
    for hora, fn in ((HORA_MANANA, job_manana), (HORA_NOCHE, job_noche), (HORA_REFLEXION, job_reflexion),
                     (HORA_REFLEXION_SEMANAL, job_reflexion_semanal)):  # la semanal revisa sola que sea domingo
        if t := _hora(hora):
            jq.run_daily(fn, time=t, name=fn.__name__)
    for item in jarvis.recordatorios():
        programar_recordatorio(app, item)
    jq.run_repeating(job_classroom, interval=CLASSROOM_CADA_MIN * 60, first=60, name="classroom")
    jq.run_repeating(job_insistir, interval=15 * 60, first=90, name="insistir")
    jq.run_repeating(job_memoria, interval=5 * 60, first=120, name="memoria")
    jq.run_repeating(job_pulso, interval=30 * 60, first=300, name="pulso")
    jq.run_repeating(job_estadisticas, interval=15 * 60, first=30, name="estadisticas")
    if t := _hora(HORA_LIMPIEZA):
        jq.run_daily(job_limpieza, time=t, name="limpieza")
    if t := _hora(HORA_EVENTOS):
        jq.run_daily(job_eventos, time=t, name="eventos")
        if t2 := _hora(HORA_AVISO_EVENTOS):
            jq.run_daily(job_aviso_eventos, time=t2, name="aviso_eventos")
    if t := _hora(HORA_LOTE_CORREOS):
        jq.run_daily(job_lote_correos, time=t, name="lote_correos")
    jq.run_repeating(job_respuestas_empleo, interval=30 * 60, first=120, name="respuestas_empleo")
    jq.run_repeating(job_pedidos_empleo, interval=20, first=30, name="pedidos_empleo")
    jq.run_repeating(job_pedidos_cv, interval=20, first=40, name="pedidos_cv")
    if t := _hora(os.environ.get("HORA_RECORDAR_APLICAR", "19:30")):
        jq.run_daily(job_recordar_aplicar, time=t, name="recordar_aplicar")
    if t := _hora(HORA_EMPLEOS):
        jq.run_daily(job_empleos, time=t, name="empleos")
        if t2 := _hora(HORA_AVISO_EMPLEOS):
            jq.run_daily(job_aviso_empleos, time=t2, name="aviso_empleos")
    log.info("Jarvis listo: mañana %s, noche %s, reflexión %s, %d recordatorios",
             HORA_MANANA, HORA_NOCHE, HORA_REFLEXION, len(jarvis.recordatorios()))


# ---------- handlers ----------

def autorizado(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == ALLOWED_CHAT_ID


_candados: dict[int, asyncio.Lock] = {}


async def responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, texto_usuario: str, con_voz: bool = False):
    """Un mensaje a la vez por chat: con concurrent_updates, dos mensajes seguidos corrían en
    paralelo con historiales cruzados y las respuestas salían revueltas."""
    async with _candados.setdefault(update.effective_chat.id, asyncio.Lock()):
        await _responder(update, ctx, texto_usuario, con_voz)


async def _responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, texto_usuario: str, con_voz: bool = False):
    chat = update.effective_chat.id
    add_msg(chat, "user", texto_usuario)
    await update.effective_chat.send_action(ChatAction.RECORD_VOICE if con_voz else ChatAction.TYPING)
    opciones: list[str] = []
    try:
        answer, opciones, pidio_voz = await llamar_deepseek(update, ctx, chat)
        con_voz = con_voz or pidio_voz
    except Exception as e:
        log.exception("deepseek")
        answer = f"⚠️ Error hablando con DeepSeek: {e}"
    # Si DeepSeek escribió los botones como texto ("[Botones: a | b]") en vez de usar la herramienta, se rescatan.
    if m := re.search(r"\n*\[Botones:\s*(.+?)\]\s*$", answer or "", flags=re.S):
        answer = answer[:m.start()].rstrip()
        opciones = opciones or [o.strip() for o in m[1].split("|") if o.strip()][:6]
    if not answer and opciones:
        answer = "¿Cuál?"
    if not answer:
        return
    add_msg(chat, "assistant", answer + (f"\n[Botones: {' | '.join(opciones)}]" if opciones else ""))
    teclado = teclado_opciones(ctx, opciones) if opciones else None
    audio = None
    if con_voz:
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        try:
            audio = await voz.hablar(answer)
        except Exception:
            log.exception("tts")
    await enviar(ctx.bot, chat, answer, teclado, audio)


async def on_voz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    m = update.message
    media = m.voice or m.audio
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        texto = await voz.transcribir(audio, getattr(media, "file_name", None) or "voz.ogg")
    except Exception as e:
        log.exception("transcripción")
        await m.reply_text(f"⚠️ No pude escuchar el audio: {e}")
        return
    if not texto:
        await m.reply_text("No se escuchó nada, we 🤷")
        return
    await m.reply_text(f"🎤 {texto}")
    await responder(update, ctx, texto, con_voz=True)


async def on_boton(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not autorizado(update):
        await q.answer()
        return
    partes = q.data.split(":")
    if partes[0] == "pln":  # plan de video/presentación: [Hazlo] [Cambiar]
        crudo = kv_get(f"pedido:{partes[2]}", "")
        if not crudo:
            await q.answer("Ese plan ya no está; pídemelo otra vez")
            return
        args = json.loads(crudo)
        kv_set(f"pedido:{partes[2]}", "")
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=None)
        if partes[1] == "go":
            r = await tool_delegar(update, ctx, args | {"confirmado": True})
            add_msg(update.effective_chat.id, "assistant", f"[Chava aprobó el plan de «{args.get('titulo')}». {r}]")
        else:
            await responder(update, ctx, f"[Quiero cambiar algo del plan de «{args.get('titulo')}» antes de que "
                                         f"lo hagas. Pregúntame qué y vuelve a proponerlo.] Plan: {args.get('plan', '')}")
        return
    if partes[0] == "pc" and partes[1] == "x":
        lista = (await remoto.pedir("listar")).get("sesiones", [])
        cual = next((x for x in lista if str(x["pid"]) == partes[2]), None)
        if not cual:
            await q.answer("Esa sesión ya estaba cerrada")
            return
        r = await remoto.pedir("cerrar", nombre=cual["nombre"])
        await q.answer("Cerrada" if r.get("ok") else "No se pudo cerrar")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🛑 Cerré «{cual['nombre']}»." if r.get("ok") else "⚠️ " + r.get("error", ""))
        return
    if partes[0] == "pon":
        kv_set("print_activo", partes[1])
        await q.answer("Impresión prendida" if partes[1] == "1" else "Impresión apagada")
        await q.edit_message_text(texto_print_estado(), parse_mode="HTML", reply_markup=teclado_print())
        return
    if partes[0] == "prn":
        ruta = ctx.bot_data.get("imprimibles", {}).get(partes[1])
        await q.answer()
        if not ruta or not Path(ruta).is_file():
            await update.effective_chat.send_message("Ese archivo ya no lo tengo a la mano; mándamelo con /print.")
            return
        await abrir_dialogo_impresion(ctx, update.effective_chat.id, Path(ruta))
        return
    if partes[0] == "prs":
        r = await asyncio.to_thread(impresora.cancelar, q.data[4:])
        await q.answer(f"Impresión: {r}"[:190])
        try:
            await q.edit_message_text(q.message.text + f"\n\n⛔ {r}")
        except Exception:
            pass
        return
    if partes[0] == "prt":
        _, key, acc = partes
        d = ctx.bot_data.get("impresiones", {}).get(key)
        if not d:
            await q.answer("Ese diálogo ya caducó; pídemelo otra vez")
            return
        if acc == "x":
            ctx.bot_data["impresiones"].pop(key, None)
            await q.answer("Cancelado")
            await q.edit_message_text("❌ Impresión cancelada.")
            return
        if acc == "go":
            if not print_activo():
                await q.answer("La impresión está apagada (/print on)", show_alert=True)
                return
            ctx.bot_data["impresiones"].pop(key, None)
            await q.answer("🖨 Va")
            ctx.application.create_task(ejecutar_impresion(ctx, update.effective_chat.id, d, q.message))
            return
        if acc in ("c1", "c0"):
            d["color"] = acc == "c1"
        elif acc == "m":
            d["copias"] = max(1, d["copias"] - 1)
        elif acc == "p":
            d["copias"] = min(20, d["copias"] + 1)
        elif acc.startswith("q-"):
            d["calidad"] = acc[2:]
        elif acc.startswith("t-"):
            d["tamano"] = acc[2:]
        await q.answer()
        try:
            await q.edit_message_text(_dlg_texto(d), parse_mode="HTML", reply_markup=_dlg_teclado(key, d))
        except Exception:
            pass  # mismo contenido (tocó la opción que ya estaba)
        return
    if partes[0] == "pmt":
        _, key, acc = partes
        d = ctx.bot_data.get("prompts", {}).get(key)
        if not d:
            await q.answer("Ese prompt ya caducó; pídelo otra vez con /prompt")
            return
        if acc == "x":
            ctx.bot_data["prompts"].pop(key, None)
            await q.answer("Descartado")
            await q.edit_message_reply_markup(reply_markup=None)
        elif acc == "otra":
            await q.answer("Redactando otra…")
            d["prompt"] = await generar_prompt(d["idea"], d["prompt"])
            await mostrar_prompt(ctx.bot, ctx, update.effective_chat.id, key, q.message)
        elif acc == "go":
            ctx.bot_data["prompts"].pop(key, None)
            await q.answer("🚀 A Claude")
            await q.edit_message_reply_markup(reply_markup=None)
            await mandar_a_claude(ctx, update.effective_chat.id, d["prompt"], [])
        return
    if partes[0] == "cla":
        kv_set("claude_activo", partes[1])
        await q.answer("Claude prendido" if partes[1] == "1" else "Claude apagado")
        await q.edit_message_text(texto_claude_estado(), parse_mode="HTML", reply_markup=teclado_claude())
        return
    if partes[0] == "ini":
        kv_set("iniciativa_nivel", partes[1])
        maximo, horas = NIVELES[partes[1]]
        await q.answer(f"Iniciativa: {partes[1]}")
        await q.edit_message_text(f"🧭 Iniciativa: <b>{partes[1]}</b> — hasta {maximo} mensajes extra al día, "
                                  f"mínimo {horas:g} h entre uno y otro.", parse_mode="HTML")
        return
    if partes[0] == "mem":
        await q.answer()
        filas = memoria.buscar("", partes[1], n=60)
        texto = f"🧠 <b>{memoria.CATEGORIAS.get(partes[1], partes[1])}</b> ({len(filas)})\n\n" + (_mem_lineas(filas) or "(vacío)")
        for i in range(0, len(texto), 4000):
            await update.effective_chat.send_message(texto[i:i + 4000], parse_mode="HTML")
        return
    if partes[0] == "anu":
        _, aid_corto, cual = partes
        row = db.execute("SELECT id, curso, texto, fechas FROM anuncios WHERE id LIKE ?", ("%" + aid_corto,)).fetchone()
        if not row:
            await q.answer("Ese anuncio ya no lo tengo")
            return
        fechas = json.loads(row[3] or "[]")
        elegidas = fechas if cual == "all" else [fechas[int(cual)]]
        hechos = []
        for f in elegidas:
            if f.get("agregado"):
                continue
            inicio, fin = f["inicio"], f.get("fin", "")
            if len(inicio) == 10 and f.get("tipo") in ("examen", "exposicion", "clase"):
                # solo trae el día: se usa la hora de la clase de esa materia ese día
                clase = next((e for e in await asyncio.to_thread(g.cal_listar, inicio, inicio)
                              if e["calendario"] == g.CAL_CLASES[0] and norm(e["titulo"]) == norm(row[1])), None)
                if clase:
                    inicio, fin = clase["inicio"], clase["fin"]
            try:
                cal = await asyncio.to_thread(g.agregar_fecha_escolar, f.get("tipo", "otro"), f"{f['titulo']}", inicio,
                                              fin, f"{row[1]} · anuncio del profe:\n{row[2][:1500]}")
                f["agregado"] = True
                hechos.append(f"{f['titulo']} → {cal}")
            except Exception as e:
                hechos.append(f"⚠️ {f['titulo']}: {e}")
        db.execute("UPDATE anuncios SET fechas=? WHERE id=?", (json.dumps(fechas, ensure_ascii=False), row[0]))
        db.commit()
        invalidar_agenda()
        await q.answer("📅 Listo" if hechos else "Ya estaba agregado")
        if hechos:
            await update.effective_chat.send_message("📅 Agregado:\n" + "\n".join(f"• {h}" for h in hechos))
            add_msg(update.effective_chat.id, "user", "[Agregué al calendario: " + "; ".join(hechos) + "]")
        try:
            await q.edit_message_reply_markup(reply_markup=teclado_anuncio(row[0], fechas))
        except Exception:
            pass
        return
    if partes[0] == "cv":  # /cv: mandar el PDF elegido
        lista = json.loads(kv_get("cv_lista", "[]"))
        k = int(partes[1]) if partes[1].isdigit() else -1
        if not 0 <= k < len(lista) or not Path(lista[k]).is_file():
            await q.answer("Ese CV ya no está; manda /cv otra vez")
            return
        await q.answer("📄 Va")
        await mandar_archivo_tg(ctx.bot, update.effective_chat.id, Path(lista[k]), caption=Path(lista[k]).name)
        return
    if partes[0] == "mail" and partes[1] == "resp":  # correo de empleo que mando la app: que KORRA ayude a contestar
        await q.answer()
        await responder(update, ctx, f"Ayúdame a contestar el correo con id {partes[2]} de mi cuenta de empleo: léelo con "
                                     "gmail_leer (cuenta 'empleo') y redáctame una respuesta corta, cordial y profesional en "
                                     "español, respetando mi perfil (si es de IT, preséntame como estudiante de software). "
                                     "Déjala lista con gmail_enviar como respuesta a ese correo (responder_a = ese id), para "
                                     "que yo la revise y le dé Enviar.")
        return
    if partes[0] == "lote":  # lote de correos de empleo: [Enviar] [Ver/quitar] [Hoy no] y [Quitar i]
        d = ce.lote_actual()
        if not d:
            await q.answer("Ese lote ya no está")
            return
        if partes[1] == "enviar":
            await q.answer("📨 Enviando…")
            await update.effective_chat.send_message(f"📨 Mandando {len(d['items'])} correo(s) poco a poco (1-2 min entre cada uno)…")
            ctx.application.create_task(enviar_lote_correos(ctx.bot))
        elif partes[1] == "no":
            ce.descartar_lote()
            await q.answer("Va, hoy no")
            await update.effective_chat.send_message("👌 Hoy no se manda nada. Esos contactos siguen pendientes para otro día.")
        elif partes[1] == "ver":
            await q.answer()
            for i, x in enumerate(d["items"]):
                await update.effective_chat.send_message(
                    f"<b>{i + 1}. Para:</b> {html.escape(x['correo'])} ({html.escape(x['empresa'])})\n<b>Asunto:</b> "
                    f"{html.escape(x['asunto'])}\n\n{html.escape(x['cuerpo'])}"[:4000], parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 Quitar el {i + 1}", callback_data=f"lote:quitar:{x['correo'][:40]}")]]))
            await update.effective_chat.send_message(texto_lote(d), parse_mode="HTML", reply_markup=teclado_lote(len(d["items"])))
        elif partes[1] == "quitar":
            i = next((k for k, x in enumerate(d["items"]) if x["correo"][:40] == ":".join(partes[2:])), None)
            if i is None:
                await q.answer("Ya no estaba")
                return
            d = ce.quitar(i)
            await q.answer("🗑 Quitado")
            if d:
                await update.effective_chat.send_message(texto_lote(d), parse_mode="HTML", reply_markup=teclado_lote(len(d["items"])))
            else:
                await update.effective_chat.send_message("Quitaste todos: hoy no se manda nada.")
        return
    if partes[0] == "emp":  # puestos: ⭐ Me interesa → CV · 🙅 no · ✅ ya apliqué · páginas
        accion, arg = partes[1], partes[2]
        chat = update.effective_chat.id
        if accion == "pag":
            texto, teclado, _ = empleos.pagina_puestos(int(arg) if arg.isdigit() else 0)
            await q.answer()
            try:
                await q.edit_message_text(texto, parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado)
            except Exception:
                await update.effective_chat.send_message(texto, parse_mode="HTML", disable_web_page_preview=True, reply_markup=teclado)
            return
        if accion in ("int", "cv"):
            if not claude_activo():
                await q.answer()
                await update.effective_chat.send_message(CLAUDE_APAGADO)
                return
            await q.answer("⭐ Te armo el CV…")
            if not await iniciar_cv(ctx, chat, arg):
                await update.effective_chat.send_message("Ese puesto ya no está en tu lista.")
            return
        estado = {"no": "descartada", "apl": "aplicada"}.get(accion)
        v = empleos.poner_estado(arg, estado) if estado else None
        if not v:
            await q.answer("Ese puesto ya no está")
            return
        if accion == "apl":
            await q.answer("✅ Guardado")
            await update.effective_chat.send_message(f"✅ Quedó como aplicado: {v['puesto']} — {v['empresa']}. ¡Suerte! Lo ves en tu app (Jobs → Aplicadas).")
            add_msg(chat, "user", f"[Ya apliqué a {v['puesto']} en {v['empresa']}]")
        else:
            await q.answer("🙅 Descartado")
        return
    if partes[0] == "evt":
        try:
            nombre = await agendar_evento(partes[2])
            await q.answer("📅 Agendado")
            await update.effective_chat.send_message(f"📅 Listo, «{nombre}» ya está en tu calendario (morado), con aviso un día y 2 h antes.")
            add_msg(update.effective_chat.id, "user", f"[Agendé el evento «{nombre}»]")
        except Exception as e:
            await q.answer("No se pudo")
            await update.effective_chat.send_message(f"⚠️ No pude agendarlo: {e}")
        return
    if partes[0] == "op_fijo" and partes[1] == "correos":
        await q.answer()
        await responder(update, ctx, "Resúmeme mis correos importantes sin leer de los últimos 3 días (las dos cuentas): "
                                     "primero los de profes/escuela/trabajo, luego lo demás; ignora promociones.")
        return
    if partes[0] == "op":
        _, key, idx = partes
        opciones = ctx.bot_data.get("opciones", {}).pop(key, None)
        if opciones is None:
            await q.answer("Esos botones ya caducaron")
            return
        elegida = opciones[int(idx)]
        await q.answer()
        try:
            if q.message.text is not None:
                await q.edit_message_text(f"{q.message.text}\n\n👉 {elegida}")
            else:  # nota de voz: se edita el pie, no el texto
                await q.edit_message_caption(f"{q.message.caption or ''}\n\n👉 {elegida}"[:1024])
        except Exception:
            log.warning("no se pudo marcar la opción elegida", exc_info=True)
        await responder(update, ctx, elegida)
        return

    if partes[0] == "mail":
        _, accion, key = partes
        borrador = ctx.bot_data.get("correos", {}).pop(key, None)
        if not borrador:
            await q.answer("Ese borrador ya no existe")
            return
        if accion == "cancel":
            await q.answer("Cancelado")
            await q.edit_message_text(q.message.text + "\n\n❌ Cancelado")
            add_msg(update.effective_chat.id, "user", "[Cancelé el correo, no se envió]")
            return
        try:
            await asyncio.to_thread(g.gmail_enviar, borrador["cuenta"], borrador["para"], borrador["asunto"],
                                    borrador["cuerpo"], borrador.get("responder_a", ""),
                                    [Path(x) for x in borrador.get("adjuntos", [])], borrador.get("cc", ""))
        except Exception as e:
            await q.answer("Falló el envío")
            await update.effective_chat.send_message(f"⚠️ No se pudo enviar: {e}")
            return
        await q.answer("Enviado 📤")
        await q.edit_message_text(q.message.text + "\n\n📤 Enviado")
        add_msg(update.effective_chat.id, "user", f"[Envié el correo «{borrador['asunto']}» a {borrador['para']}]")
        return

    if partes[0] == "conf":
        _, accion, key = partes
        pendiente = ctx.bot_data.get("confirmar", {}).pop(key, None)
        if not pendiente:
            await q.answer("Eso ya caducó")
            return
        if accion == "no":
            await q.answer("Cancelado")
            await q.edit_message_text(q.message.text + "\n\n❌ Cancelado")
            add_msg(update.effective_chat.id, "user", "[Cancelé esa acción, no se hizo]")
            return
        try:
            res = await ejecutar_confirmada(pendiente)
        except Exception as e:
            await q.answer("Falló")
            await update.effective_chat.send_message(f"⚠️ No se pudo: {e}")
            return
        await q.answer("Hecho")
        await q.edit_message_text(q.message.text + f"\n\n✅ {res}")
        add_msg(update.effective_chat.id, "user", f"[Confirmé: {q.message.text[:200]} → {res}]")
        return

    if partes[0] == "tarea":
        _, accion, tid = partes
        t = tarea(tid)
        if not t:
            await q.answer("No encontré esa tarea")
            return
        if accion == "hecha":
            await marcar_tarea_hecha(tid)
            await q.answer("🔥 Anotado")
            await q.edit_message_reply_markup(reply_markup=None)
            add_msg(update.effective_chat.id, "user", f"[Marqué como entregada «{t['titulo']}»]")
            await update.effective_chat.send_message(f"✅ «{t['titulo']}» entregada. Ya no te molesto con esa 😎")
        elif accion == "luego":
            db.execute("UPDATE tareas SET pospuesta=? WHERE id=?", (time.time() + 90 * 60, tid))
            db.commit()
            await q.answer("Va… en hora y media te vuelvo a caer 😒")
            await q.edit_message_reply_markup(reply_markup=None)
        elif accion == "claude":
            await q.answer()
            await q.edit_message_reply_markup(reply_markup=None)
            carpeta = WORKSPACE / (t.get("carpeta") or "")
            archivos = [p.name for p in carpeta.iterdir() if p.is_file()] if t.get("carpeta") and carpeta.is_dir() else []
            await responder(update, ctx,
                f"[Quiero que Claude haga la tarea de Classroom «{t['titulo']}» de {t.get('materia') or t.get('curso')}, "
                f"entrega {(t.get('entrega') or 'sin fecha')[:16]}. Carpeta: CETI COLOMOS/{t.get('carpeta')} "
                f"(archivos ahí: {', '.join(archivos) or 'ninguno'}; las instrucciones completas están en instrucciones.md). "
                f"Instrucciones del profe: {(t.get('descripcion') or '')[:2500]}\n"
                "Si de verdad falta algo para hacerla, pregúntame con botones; si no, delégala a Claude en esa carpeta "
                "(materia y parcial correspondientes) diciéndole que lea instrucciones.md y los adjuntos.]")
        return

    if partes[0] == "ses":
        if partes[1] == "x":
            ctx.chat_data.pop("sesion", None)
            await q.answer("Cancelado")
            await q.edit_message_reply_markup(reply_markup=None)
            return
        lista = (await worker.get("/sesiones", params={"n": 40})).json()
        ses = next((x for x in lista if x["id"] == partes[1]), None)
        if not ses:
            await q.answer("Esa sesión ya no existe", show_alert=True)
            return
        await q.answer()
        await pedir_texto_sesion(ctx, update.effective_chat.id, ses)
        return

    _, accion, job_id = partes
    if accion == "cancel":
        r = await worker.post(f"/jobs/{job_id}/cancel")
        await q.answer(f"Cancelando… ({r.json().get('status', '?')})")
        return
    if accion == "files":
        r = await worker.get(f"/jobs/{job_id}")
        if r.status_code != 200:
            await q.answer("Ya no tengo ese trabajo a la mano; pídeme el archivo por su nombre", show_alert=True)
            return
        await q.answer("Van…")
        await q.edit_message_reply_markup(reply_markup=None)
        await enviar_archivos_job(ctx, update.effective_chat.id, r.json())
        return
    if accion == "cont":
        r = await worker.get(f"/jobs/{job_id}")
        job = r.json() if r.status_code == 200 else {}
        if not job.get("session_id"):
            await q.answer("Ya no tengo ese trabajo a la mano: búscalo en /sesiones", show_alert=True)
            return
        await q.answer()
        await pedir_texto_sesion(ctx, update.effective_chat.id, {
            "id": job["session_id"], "cwd": job["cwd"], "titulo": jobs_info().get(job_id, {}).get("titulo", "tarea")})
        return
    # Primero lo que guardó el bot (sobrevive a reinicios); si no, lo que tenga el worker.
    info = jobs_info().get(job_id)
    if accion == "later":
        if not info:
            await q.answer("Ya no tengo los datos de ese trabajo")
            return
        ctx.job_queue.run_once(job_reintento, 3600, data=job_id, name=f"reintento-{job_id}")
        await q.answer("Va, lo reintento en una hora")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"⏰ Reintento «{info.get('titulo', 'tarea')}» a las "
                                   f"{(jarvis.ahora() + timedelta(hours=1)):%H:%M}.")
        return
    r = await worker.get(f"/jobs/{job_id}")
    job = r.json() if r.status_code == 200 else {}
    if not info and not job:
        await q.answer("Ya no tengo los datos de ese trabajo (se reinició el servidor)")
        return
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    prompt = (info or job)["prompt"]
    cwd = (info or job)["cwd"]
    titulo = (info or {}).get("titulo", "tarea")
    if accion == "redo":
        await lanzar_job(ctx, update.effective_chat.id, prompt, cwd, f"{titulo} (otra vez)",
                         enviar=info["enviar"] if info else True, tipo=(info or {}).get("tipo", "otro"),
                         escuela=(info or {}).get("escuela", False))
    elif accion == "fix":
        archivos = ", ".join((job.get("entregables") or job.get("changed", []))[:10]) or "(ninguno)"
        await responder(update, ctx,
            f"[Quiero corregir algo del resultado de «{titulo}» (carpeta {cwd}, archivos: {archivos}). "
            "Pregúntame qué le cambio y luego delega la corrección a Claude, diciéndole qué archivos "
            "editar y el prompt original como contexto.]\n"
            f"Prompt original: {prompt[:3000]}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    texto = update.message.text.strip()
    if texto.startswith(g.REDIRECT_URI.rstrip("/")) and ctx.bot_data.get("oauth"):
        cuenta, flow = ctx.bot_data.pop("oauth")
        try:
            correo = await asyncio.to_thread(g.terminar_conexion, cuenta, flow, texto)
            await update.message.reply_text(f"✅ Cuenta «{cuenta}» conectada: {correo}")
            faltan = await asyncio.to_thread(g.permisos_faltantes, cuenta)
            if faltan:
                await update.message.reply_text(
                    "⚠️ Pero NO palomeaste estos permisos: " + ", ".join(faltan) +
                    f"\nSin ellos no puedo usar esa parte. Vuelve a mandar /google {cuenta} y en la pantalla de "
                    "Google marca TODAS las casillas (o «Seleccionar todo»).")
            if cuenta == g.CUENTA_CLASSROOM:
                await update.message.reply_text("📚 Voy a revisar tu Classroom, dame un minuto…")
                ctx.application.job_queue.run_once(job_classroom, when=5)
        except Exception as e:
            log.exception("oauth")
            await update.message.reply_text(f"⚠️ No se pudo conectar: {e}\nIntenta otra vez con /google {cuenta}")
        return
    pendiente = ctx.chat_data.pop("sesion", None)
    if pendiente and time.time() - pendiente["ts"] < SESION_ESPERA_S:
        await continuar_sesion(ctx, update.effective_chat.id, pendiente, texto)
        return
    await responder(update, ctx, texto)


async def cmd_google(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if not g.configurado():
        await update.effective_chat.send_message(
            "Faltan las credenciales de Google (secrets/google_client_<cuenta>.json). Sigue el README, sección Google.")
        return
    if not ctx.args:
        estado = []
        for c in g.CUENTAS:
            if c in g.conectadas():
                try:
                    faltan = await asyncio.to_thread(g.permisos_faltantes, c)
                    estado.append(f"✅ {c}: {await asyncio.to_thread(g.email, c)}" +
                                  (f"\n   ⚠️ faltan permisos: {', '.join(faltan)} → /google {c} y marca todas las casillas" if faltan else ""))
                except Exception as e:
                    estado.append(f"⚠️ {c}: error ({e}) — reconecta con /google {c}")
            else:
                estado.append(f"❌ {c}: sin conectar — /google {c}")
        await update.effective_chat.send_message(
            "\n".join(estado) + f"\n\nClassroom: {g.CUENTA_CLASSROOM} · Calendario principal: {g.CUENTA_CALENDARIO}")
        return
    cuenta = g.real(ctx.args[0].lower())  # /google empleo = /google personal (mismo Gmail)
    if cuenta not in g.CUENTAS:
        await update.effective_chat.send_message(f"Cuentas válidas: {', '.join(g.CUENTAS)}")
        return
    if not g.configurado(cuenta):
        await update.effective_chat.send_message(
            f"Falta secrets/google_client_{cuenta}.json en la Pi (el JSON del cliente OAuth de esa cuenta).")
        return
    url, flow = await asyncio.to_thread(g.iniciar_conexion, cuenta)
    ctx.bot_data["oauth"] = (cuenta, flow)
    await update.effective_chat.send_message(
        f"🔐 Conectar cuenta «{cuenta}»:\n\n1. Abre este link e inicia sesión con esa cuenta.\n"
        "2. Si sale «Google no verificó esta app» → Configuración avanzada → Ir a … (no seguro). Es tu propia app.\n"
        "3. Acepta los permisos.\n4. Te va a mandar a una página que NO carga (localhost). Es normal: "
        "copia la URL completa de la barra del navegador y pégamela aquí.\n\n" + url,
        disable_web_page_preview=True)


async def cmd_parcial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args and re.fullmatch(r"[Pp]?[1-3]", ctx.args[0]):
        kv_set("parcial", "P" + ctx.args[0][-1])
    await update.effective_chat.send_message(f"Parcial actual: {parcial_actual()}\n(cámbialo con /parcial 2)")


async def cmd_tareas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    ts = tareas_pendientes()
    if not ts:
        await update.effective_chat.send_message("Sin tareas pendientes 😎")
        return
    for t in ts[:10]:
        await update.effective_chat.send_message(
            f"📚 {t['materia'] or t['curso']}: {t['titulo']}\n🗓 {(t['entrega'] or 'sin fecha')[:16].replace('T', ' ')}",
            reply_markup=teclado_tarea(t["id"]))


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    m = update.message
    if m.document and (m.document.file_size or 0) > 20 * 1024 * 1024:  # límite de descarga de la API de bots
        await m.reply_text(f"📦 «{m.document.file_name}» pesa más de 20 MB y Telegram no me deja bajarlo. "
                           "Cópialo desde la Mac a Samba → Documentos.")
        return
    if m.document:
        tg_file, nombre = await m.document.get_file(), m.document.file_name or "archivo"
    else:  # foto: la de mayor resolución
        tg_file, nombre = await m.photo[-1].get_file(), "foto.jpg"
    base = f"{time.strftime('%Y%m%d_%H%M%S')}_{Path(nombre).name}"
    destino = _entrada() / base
    n = 1
    while destino.exists():  # varios archivos en el mismo segundo
        destino = _entrada() / f"{time.strftime('%Y%m%d_%H%M%S')}_{n}_{Path(nombre).name}"
        n += 1
    destino.parent.mkdir(parents=True, exist_ok=True)
    await tg_file.download_to_drive(destino)

    # Si manda varios archivos seguidos (o un álbum), se juntan y KORRA contesta UNA vez.
    lote = ctx.chat_data.setdefault("lote", {"archivos": [], "textos": []})
    lote["archivos"].append(destino.name)
    if m.caption:
        lote["textos"].append(m.caption)
    for job in ctx.job_queue.get_jobs_by_name(f"lote-{update.effective_chat.id}"):
        job.schedule_removal()
    ctx.job_queue.run_once(procesar_lote, when=4, name=f"lote-{update.effective_chat.id}",
                           data=update, chat_id=update.effective_chat.id)


async def procesar_lote(ctx: ContextTypes.DEFAULT_TYPE):
    update: Update = ctx.job.data
    lote = ctx.chat_data.pop("lote", None)
    if not lote or not lote["archivos"]:
        return
    pie = "\n".join(lote["textos"]).strip()
    if re.match(r"^/print(@\w+)?\b", pie, re.I):
        for nombre in lote["archivos"][:5]:
            await abrir_dialogo_impresion(ctx, update.effective_chat.id, _entrada() / nombre)
        return
    if m := re.match(r"^/claude(@\w+)?\b\s*", pie, re.I):
        # Telegram no trata como comando lo que va en el pie de una foto: aquí se hace a mano.
        await mandar_a_claude(ctx, update.effective_chat.id, pie[m.end():], lote["archivos"])
        return
    lista = "\n".join(f"- {a}" for a in lote["archivos"])
    texto = (f"[Te mandé {len(lote['archivos'])} archivo(s) por Telegram; están en la bandeja de entrada "
             f"(CETI COLOMOS/{ENTRADA_DIR}):\n{lista}\n"
             "Si son para una tarea, delega a Claude (espacio escuela) diciéndole que los copie a la carpeta del parcial. "
             "Si son cosas personales (recibos, documentos), delega a Claude con espacio personal para que los "
             "acomode en JARVIS/archivos.]\n"
             + ("\n".join(lote["textos"]) or "(sin mensaje: si no es obvio qué hacer, pregúntame con botones)"))
    await responder(update, ctx, texto)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        await update.effective_chat.send_message(f"No autorizado. Tu chat id es {update.effective_chat.id}")
        return
    await cmd_ayuda(update, ctx)


# (comando, descripción corta para el menú de Telegram). El orden es el del menú.
COMANDOS = [
    ("ayuda", "Lista de comandos"),
    ("info", "Qué es KORRA y cómo funciona"),
    ("habilidades", "Qué sabe hacer KORRA (sus skills)"),
    ("claude", "Mandar algo directo a Claude Code"),
    ("prompt", "Redactar un buen prompt para Claude"),
    ("print", "Impresora: on/off, estado, imprimir"),
    ("tinta", "Tinta y papel de la impresora"),
    ("hoy", "Mi agenda de hoy"),
    ("manana", "Mi agenda de mañana"),
    ("semana", "Agenda de los próximos 7 días"),
    ("tareas", "Tareas de Classroom pendientes"),
    ("correos", "Correos importantes sin leer"),
    ("eventos", "Eventos de software en GDL"),
    ("puestos", "Todos tus puestos: ⭐ Me interesa → CV a la medida"),
    ("cv", "Tus CVs, o uno a la medida: /cv + detalles del puesto"),
    ("lote", "Correos de empleo de hoy (con tu CV)"),
    ("uso", "Uso de Claude y saldo de DeepSeek"),
    ("iniciativa", "Qué tanto te escribo por mi cuenta"),
    ("silencio", "Que no te escriba unas horas"),
    ("servidor", "Estado del servidor"),
    ("respaldos", "Cómo va el respaldo de iCloud"),
    ("pc", "Sesión de Claude Code para tu iPhone"),
    ("limpiar", "Limpiar la RAM del asistente"),
    ("estado", "Trabajos de Claude en curso"),
    ("sesiones", "Sesiones pasadas de Claude (continuarlas)"),
    ("continuar", "Seguir la última sesión de Claude"),
    ("skills", "Skills globales de Claude: ver e instalar"),
    ("cancelar", "Cancelar el trabajo de Claude"),
    ("recordatorios", "Recordatorios pendientes"),
    ("memoria", "Lo que sé de ti"),
    ("notas", "Mi libreta de notas"),
    ("materias", "Materias del semestre"),
    ("parcial", "Ver o cambiar el parcial actual"),
    ("semestre", "Semestre activo"),
    ("nuevo_semestre", "Crear el siguiente semestre"),
    ("google", "Estado/conexión de cuentas Google"),
    ("olvidar", "Borrar la plática (no la memoria)"),
]

AYUDA = """<b>🤖 KORRA — comandos</b>

<b>Mi día</b>
/hoy · /manana · /semana — tu agenda (todas tus cuentas y calendarios)
/correos — correos importantes sin leer, con botón para resumirlos
/eventos — eventos de software en GDL que te encontré, con botón para agendar
   <code>/eventos buscar</code> — que Claude busque ahorita (también lo hace solo cada mañana)
/puestos — todos tus puestos; ⭐ Me interesa → te armo el CV a la medida y te lo mando; luego ✅ Ya apliqué
/cv — tus CVs para recibirlos en PDF; <code>/cv</code> + detalles del puesto → uno a la medida
   <code>/puestos buscar</code> — que Claude busque ahorita (también lo hace solo lunes y jueves)
/lote — correos de empleo del día (personalizados, con tu CV) para aprobar con un botón; <code>/lote ya</code> lo arma ahorita

<b>Escuela</b>
/tareas — tareas de Classroom pendientes, con botones
/materias — materias del semestre activo
/parcial — ver el parcial actual · <code>/parcial 2</code> lo cambia
/semestre — semestre activo y los anteriores
/nuevo_semestre <i>materias</i> — crea el siguiente semestre
   ej. <code>/nuevo_semestre Física, Cálculo (Prof. Pérez) grupo: I-2Me</code>

<b>Claude (trabajos pesados)</b>
/prompt <i>idea</i> — te redacto un buen prompt para Claude; con 🚀 se lo mando, 🔁 otra versión
/claude <b>off</b> · <b>on</b> — apaga/prende a Claude (apagado no gasta tu límite) · /claude solo = estado
/claude <i>lo que quieres</i> — va DIRECTO a Claude, sin pasar por mí
   ej. <code>/claude haz la tarea de Discretas P2 en Word con portada</code>
   <code>/claude personal</code> … para tus archivos personales (Documentos/Personal)
   (también puedes responder a un mensaje con /claude para pasárselo)
/estado — trabajos en curso y recientes
/pc <i>materia o carpeta</i> — abre Claude Code en esa carpeta para que TÚ lo uses desde la app de Claude en el iPhone
   (no se cae si se va el SSH) · ej. <code>/pc cultura</code>, <code>/pc discretas p3</code>, <code>/pc servicios</code>,
   <code>/pc</code> solo = ver las abiertas y cerrarlas
/sesiones — trabajos pasados de Claude; toca uno y escribe qué más quieres (recuerda todo lo que hizo)
/continuar <i>qué hacer</i> — sigue la última sesión · ej. <code>/continuar ahora renderiza el MP4</code>
/skills — skills y plugins globales que usa Claude · <code>/skills remotion-dev/skills</code> instala
   (también jala pegar <code>/claude npx skills add …</code>: se instalan global, sin gastar límite)
/cancelar — cancela el que está corriendo · <code>/cancelar id</code> uno en específico

<b>Yo (KORRA)</b>
/memoria — lo que sé de ti
/notas — mi libreta · <code>/notas nombre</code> lee una
/recordatorios — recordatorios pendientes
/olvidar — borra la plática (mi memoria se queda)

<b>Impresora de la casa</b>
/tinta (o /papel) — nivel de tinta por color y si hay papel
/print <b>on</b> · <b>off</b> — prende/apaga la impresión (apagada, nadie imprime) · /print solo = estado y tinta
Manda un archivo con el pie <code>/print</code>, responde a un archivo con <code>/print</code>, o dime
<i>"imprime la tarea de Discretas a color, 2 copias"</i>. Siempre eliges color/B-N, copias y calidad con botones.

<b>Mi iniciativa</b>
/iniciativa — qué tanto te escribo por mi cuenta (off · baja · media · alta)
/silencio <i>horas</i> — que no te escriba por iniciativa unas horas · <code>/silencio 0</code> lo quita

<b>Sistema</b>
/uso — cuánto llevas de tu límite de Claude (sesión y semana) y saldo de DeepSeek
/servidor — RAM, disco, temperatura y si todo está arriba
/respaldos — cómo va el respaldo de iCloud (cuándo corrió, si salió bien, cuánto pesa)
/limpiar — suelta RAM, olvida botones viejos y mata procesos atorados (también corre solo cada madrugada)

<b>Cuentas</b>
/google — estado de tus cuentas y permisos
<code>/google escuela</code> · <code>/google personal</code> — conectar o reconectar

<b>Sin comandos, nomás pídemelo</b>
• Tareas: <i>"hazme la tarea de Discretas P2 en Word con portada"</i>
• Calendario: <i>"el lunes dentista a las 10"</i>, <i>"gym lunes, miércoles y viernes 7am"</i>, <i>"¿cuándo tengo libre el jueves?"</i>
• Classroom: <i>"¿qué me falta entregar?"</i>, <i>"¿qué anunció el profe de Discretas?"</i>, <i>"¿cómo voy?"</i>
• Correos: <i>"mándale mi tarea al profe de Cultura"</i>, <i>"archiva las promociones"</i>
• Correos: <i>"¿tengo correos de profes?"</i>, <i>"guárdame todos los recibos de nómina"</i>
• Recordatorios: <i>"recuérdame el jueves a las 6 estudiar"</i>
• Audios: mándame nota de voz y te contesto con voz
• Archivos/fotos: mándamelos con un mensaje de qué hacer"""


INFO = """<b>🤖 KORRA — Knowledge Orchestration &amp; Reasoning Response Assistant</b>
Asistente personal de Chava que vive en una Raspberry Pi en su casa y se usa desde Telegram.

<b>¿Cómo funciona?</b>
1. Le escribes o le mandas audio por <b>Telegram</b>.
2. El "cerebro" (<b>DeepSeek</b>) platica, decide y usa herramientas: calendario, correo, Classroom, memoria, impresora…
3. Si hay trabajo pesado (hacer una tarea en Word, investigar, organizar archivos) se lo pasa a <b>Claude Code</b>,
   que trabaja en las carpetas del servidor y regresa los archivos por Telegram.
4. Tus carpetas se ven desde la Mac por <b>Samba</b>, e iCloud se respalda cada noche en la Pi.

<b>Qué hace</b>
• Tareas de la escuela con portada, en Word/PDF/PowerPoint
• Classroom: avisa tareas y anuncios nuevos, las agenda y te insiste antes de la entrega
• Google Calendar con colores (clases, exámenes, tareas 🟡🟢🔴) y Gmail
• Memoria propia: aprende de ti y busca en todo lo que han platicado
• Iniciativa: te escribe sola (seguimientos, exámenes, eventos), respetando tu horario
• Eventos de software y ferias de empleo en Guadalajara
• Notas de voz de ida y vuelta, recordatorios, imprimir en la impresora de la casa

<b>Tecnologías</b>
🖥 <b>Hardware:</b> Raspberry Pi 5 (4 GB) en casa
🐳 <b>Docker Compose:</b> contenedores aislados (bot, worker, finanzas, panel, Samba, Cloudflare)
🐍 <b>Python 3.12:</b> python-telegram-bot, FastAPI, asyncio
🧠 <b>IA:</b> DeepSeek (plática y herramientas, function calling) · Claude Code (trabajo pesado, con skills)
🎤 <b>Voz:</b> Whisper en Groq (voz→texto) · Edge TTS (texto→voz) · ffmpeg
🗄 <b>Datos:</b> SQLite con migraciones + FTS5 (buscador) · memoria en capas
🔗 <b>APIs:</b> Google Calendar, Gmail, Classroom y Drive (OAuth 2.0) · Telegram Bot API
☁️ <b>Red:</b> Samba en casa y Tailscale · respaldo diario de iCloud · vigilante del servidor
🖨 <b>Impresión:</b> CUPS + IPP (HP Smart Tank)
📄 <b>Documentos:</b> LibreOffice, pandoc, python-docx

<b>Seguridad</b>
Solo responde a un chat autorizado · contenedores sin root ni privilegios · ningún puerto expuesto ·
acciones delicadas (enviar correos, borrar eventos, imprimir) se confirman con botón.

/ayuda para ver todos los comandos."""


async def cmd_habilidades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/habilidades → lista · /habilidades <nombre> → manual completo."""
    if not autorizado(update):
        return
    if ctx.args:
        texto = habilidades.abrir(ctx.args[0])
        for i in range(0, len(texto), 4000):
            await update.effective_chat.send_message(texto[i:i + 4000])
        return
    lineas = [f"• <b>{h['nombre']}</b>{' ✍️' if h['origen'] == 'propia' else ''} — {html.escape(h['cuando'])}"
              for h in habilidades.todas().values()]
    await update.effective_chat.send_message(
        "🧩 <b>Habilidades de KORRA</b> (las abre cuando las necesita)\n\n" + "\n".join(lineas)
        + "\n\n✍️ = propias (KORRA/habilidades, se ven por Samba). Para enseñarle una: <i>\"de ahora en adelante "
          "cuando te pida X, hazlo así…\"</i> · <code>/habilidades nombre</code> para leer una.", parse_mode="HTML")


async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await update.effective_chat.send_message(INFO, parse_mode="HTML")


async def cmd_ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await update.effective_chat.send_message(AYUDA, parse_mode="HTML")


async def cmd_materias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await update.effective_chat.send_message(f"{semestre_actual()}:\n• " + "\n• ".join(materias()))


async def cmd_semestre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        await update.effective_chat.send_message(f"Activo: {semestre_actual()}\nTodos: {', '.join(semestres())}")


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    jobs = (await worker.get("/jobs")).json()[:8]
    if not jobs:
        await update.effective_chat.send_message("Sin trabajos recientes.")
        return
    lineas = [f"{j['id']} · {j['status']} · {j['cwd']}" for j in jobs]
    await update.effective_chat.send_message("\n".join(lineas))


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if not ctx.args:
        jobs = [j for j in (await worker.get("/jobs")).json() if j["status"] in ("queued", "running")]
        if not jobs:
            await update.effective_chat.send_message("No hay nada corriendo.")
            return
        ctx.args = [jobs[0]["id"]]
    r = await worker.post(f"/jobs/{ctx.args[0]}/cancel")
    await update.effective_chat.send_message(f"{ctx.args[0]}: {r.json().get('status', r.text)}")


async def cmd_olvidar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        db.execute("DELETE FROM msgs WHERE chat=?", (update.effective_chat.id,))
        db.commit()
        await update.effective_chat.send_message("🧹 Historial borrado.")


def _mem_lineas(filas: list[dict]) -> str:
    return "\n".join(f"• <code>#{r['id']}</code> {'★' * r['importancia']} {html.escape(r['texto'][:220])}"
                     + (f" <i>(hasta {r['vigente_hasta']})</i>" if r["vigente_hasta"] else "")
                     + (" <i>[vencido]</i>" if r["estado"] == "vencido" else "") for r in filas)


def teclado_memoria() -> InlineKeyboardMarkup:
    botones = [InlineKeyboardButton(f"{memoria.CATEGORIAS.get(c, c)[:18]} ({n})", callback_data=f"mem:{c}")
               for c, n in memoria.conteo().items()]
    return InlineKeyboardMarkup([botones[i:i + 2] for i in range(0, len(botones), 2)])


async def cmd_memoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/memoria → núcleo + categorías con botones · /memoria <palabras> → busca."""
    if not autorizado(update):
        return
    if ctx.args:
        q = " ".join(ctx.args)
        filas = memoria.buscar(q, incluir_viejos=True, n=20)
        await update.effective_chat.send_message(
            (f"🔎 <b>{html.escape(q)}</b>\n\n" + _mem_lineas(filas)) if filas else "No encontré nada con eso en mi memoria.",
            parse_mode="HTML")
        return
    total = sum(memoria.conteo().values())
    nucleo = memoria.buscar("", n=400)
    nucleo = [r for r in nucleo if r["importancia"] == 3]
    texto = (f"🧠 <b>Mi memoria</b>: {total} datos\n\n<b>Núcleo</b> (lo que siempre tengo en la cabeza):\n"
             + _mem_lineas(nucleo[:25])
             + "\n\nToca una categoría para ver todo. <code>/memoria palabra</code> busca.\n"
               "Para corregir: <i>\"KORRA, el #12 está mal, es…\"</i> o corrígeme aquí mismo.")
    await update.effective_chat.send_message(texto[:4000], parse_mode="HTML", reply_markup=teclado_memoria())


async def cmd_notas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    if ctx.args:
        await update.effective_chat.send_message(jarvis.nota_leer(" ".join(ctx.args))[:4000])
    else:
        await update.effective_chat.send_message("📓 " + (", ".join(jarvis.notas_listar()) or "sin notas") +
                                                 "\n\n/notas <nombre> para leer una")


async def cmd_recordatorios(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorizado(update):
        items = jarvis.recordatorios()
        await update.effective_chat.send_message(
            "\n".join(f"⏰ {r['cuando'][:16].replace('T', ' ')} — {r['texto']} ({r['id']})" for r in items)
            or "Sin recordatorios.")


async def cmd_nuevo_semestre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    lista = " ".join(ctx.args).strip()
    if not lista:
        await update.effective_chat.send_message(
            "Uso: /nuevo_semestre Materia 1, Materia 2 (Profesor), …\n"
            "Opcional: agrega 'grupo: X' para actualizar datos.md."
        )
        return
    actual = semestre_actual()
    n = int(actual.split("_")[1]) + 1 if actual else 1
    nuevo = f"SEMESTRE_{n}"
    prompt = f"""Prepara el nuevo semestre {nuevo} en la raíz de CETI COLOMOS (tu directorio actual).
1. Crea la carpeta {nuevo}/.
2. Copia {actual}/Z99_Datos_documentos y {actual}/skills a {nuevo}/ (sin __pycache__).
3. Con esta lista que dictó el usuario: «{lista}»
   - crea una carpeta por materia en MAYÚSCULAS con acentos (como en {actual}), cada una con P1, P2 y P3;
   - actualiza {nuevo}/Z99_Datos_documentos/datos.md: sección de profesores con las materias nuevas
     (si no dio profesor, deja "POR DEFINIR"), y grupo/fecha si los dio.
4. Corre `python3 {nuevo}/Z99_Datos_documentos/generar_portadas.py` para generar las portadas.
5. NO modifiques ni borres nada de {actual}.
Al final resume qué materias creaste y qué datos faltan por llenar en datos.md."""
    await lanzar_job(ctx, update.effective_chat.id, prompt, "", f"Nuevo {nuevo}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["ayuda", "comandos", "help"], cmd_ayuda))
    app.add_handler(CommandHandler(["info", "acerca"], cmd_info))
    app.add_handler(CommandHandler("habilidades", cmd_habilidades))
    app.add_handler(CommandHandler("materias", cmd_materias))
    app.add_handler(CommandHandler("semestre", cmd_semestre))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("olvidar", cmd_olvidar))
    app.add_handler(CommandHandler("nuevo_semestre", cmd_nuevo_semestre))
    app.add_handler(CommandHandler("memoria", cmd_memoria))
    app.add_handler(CommandHandler("notas", cmd_notas))
    app.add_handler(CommandHandler("recordatorios", cmd_recordatorios))
    app.add_handler(CommandHandler("google", cmd_google))
    app.add_handler(CommandHandler("parcial", cmd_parcial))
    app.add_handler(CommandHandler("tareas", cmd_tareas))
    app.add_handler(CommandHandler("uso", cmd_uso))
    app.add_handler(CommandHandler("iniciativa", cmd_iniciativa))
    app.add_handler(CommandHandler("silencio", cmd_silencio))
    app.add_handler(CommandHandler("claude", cmd_claude))
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("sesiones", cmd_sesiones))
    app.add_handler(CommandHandler("continuar", cmd_continuar))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler(["print", "imprimir"], cmd_print))
    app.add_handler(CommandHandler(["tinta", "papel", "impresora"], cmd_tinta))
    app.add_handler(CommandHandler("servidor", cmd_servidor))
    app.add_handler(CommandHandler("respaldos", cmd_respaldos))
    app.add_handler(CommandHandler("pc", cmd_pc))
    app.add_handler(CommandHandler(["limpiar", "cleanram", "ram"], cmd_limpiar))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("manana", cmd_manana))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("correos", cmd_correos))
    app.add_handler(CommandHandler("eventos", cmd_eventos))
    app.add_handler(CommandHandler(["puestos", "empleos"], cmd_puestos))
    app.add_handler(CommandHandler("cv", cmd_cv))
    app.add_handler(CommandHandler("lote", cmd_lote))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voz))
    app.add_handler(CallbackQueryHandler(on_boton))
    log.info("Bot iniciado. Semestre activo: %s", semestre_actual())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
