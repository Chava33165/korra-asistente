"""
Espacio propio del asistente (~/KORRA en la Pi, /jarvis en el contenedor).

    personalidad.md      cómo habla (lo editas tú; él no lo toca)
    perfil.md            quién es Chava, en corto: siempre va en el prompt; lo reescribe la reflexión semanal
    memoria.md           vista de la memoria (SQLite, ver memoria.py)
    notas/<nombre>.md    su libreta: listas, planes, ideas, lo que quiera
    diario/AAAA-MM-DD.md su reflexión de cada noche sobre el día
    diario/semanas/      su reflexión de cada domingo sobre la semana
    recordatorios.json   recordatorios pendientes
"""
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(os.environ.get("JARVIS_DIR", "/jarvis"))
TZ = ZoneInfo(os.environ.get("TZ", "America/Mexico_City"))
MEMORIA = BASE / "memoria.md"
PERSONALIDAD = BASE / "personalidad.md"
PERFIL = BASE / "perfil.md"
MAX_PERFIL = 3000  # tope duro (patrón "bloque de memoria" de Letta): que no crezca sin control
NOTAS = BASE / "notas"
DIARIO = BASE / "diario"
RECORDATORIOS = BASE / "recordatorios.json"

PERSONALIDAD_DEFAULT = """# Personalidad

Te llamas {nombre}. Eres el asistente personal de Salvador ("Chava"), como su propio Jarvis,
pero con alma de compa mexicano de toda la vida.

## Cómo hablas
- Como un compa de confianza de Guadalajara: "we", "wey", "carnal", "qué pedo", "neta",
  "chido", "ahí va", "no manches", "ya valió", "a huevo", "aguas", "cámara".
- Se valen groserías con naturalidad, como entre amigos ("no mames", "está cabrón",
  "pinche tarea"), sin forzarlas en cada frase y nunca para humillarlo de verdad.
- Carrilla ligera sí; si lo notas bajoneado, estresado o cansado, bájale y apóyalo en serio.
- Mensajes cortos, como de WhatsApp. Nada de listas eternas ni tono de robot corporativo.
  Nada de "¡Claro! Estoy aquí para ayudarte".
- Emojis con moderación.

## Cómo eres
- Leal, directo y honesto: si algo está mal o es mala idea, se lo dices sin rodeos.
- Te importa que le vaya bien en el CETI, pero también que duerma, coma y se divierta.
- Tienes memoria propia: usas lo que sabes de él con naturalidad ("¿qué onda con el examen
  de Discretas que tenías?"), sin recitarle su ficha.
- Tienes iniciativa: propones cosas, te acuerdas de pendientes, preguntas cómo le fue.
- Con el trabajo no hay relajo: cuando hay que hacer una tarea, se hace bien.
- Nunca inventes datos. Si no sabes, dilo.
"""

MEMORIA_DEFAULT = """# Lo que sé de Chava

## Quién es
- Salvador Antonio Pantoja Vera, "Chava". Estudia en el CETI Colomos (registro 26310453).

## Escuela

## Gustos y cosas personales

## Cómo le gusta que le hable / trabaje

## Pendientes y cosas a darle seguimiento
"""


def init(nombre: str):
    NOTAS.mkdir(parents=True, exist_ok=True)
    DIARIO.mkdir(parents=True, exist_ok=True)
    if not PERSONALIDAD.exists():
        PERSONALIDAD.write_text(PERSONALIDAD_DEFAULT.format(nombre=nombre), encoding="utf-8")
    if not MEMORIA.exists():
        MEMORIA.write_text(MEMORIA_DEFAULT, encoding="utf-8")
    if not RECORDATORIOS.exists():
        RECORDATORIOS.write_text("[]", encoding="utf-8")


def ahora() -> datetime:
    return datetime.now(TZ)


def personalidad() -> str:
    return PERSONALIDAD.read_text(encoding="utf-8") if PERSONALIDAD.exists() else ""


def perfil() -> str:
    return PERFIL.read_text(encoding="utf-8")[:MAX_PERFIL] if PERFIL.exists() else ""


def perfil_escribir(texto: str):
    """La reflexión semanal lo reescribe; la versión anterior queda en _archivo/ (nunca se pierde)."""
    texto = texto.strip()[:MAX_PERFIL]
    if PERFIL.exists():
        archivo = BASE / "_archivo"
        archivo.mkdir(exist_ok=True)
        (archivo / f"perfil-{ahora():%Y-%m-%d}.md").write_text(PERFIL.read_text(encoding="utf-8"), encoding="utf-8")
    PERFIL.write_text(texto + "\n", encoding="utf-8")


def memoria() -> str:
    return MEMORIA.read_text(encoding="utf-8") if MEMORIA.exists() else ""


def _safe_name(nombre: str) -> str:
    nombre = re.sub(r"[^\w\- áéíóúñÁÉÍÓÚÑ]", "", nombre).strip().replace(" ", "_")
    return (nombre or "nota")[:60]


# ---------- notas ----------

def notas_listar() -> list[str]:
    return sorted(p.stem for p in NOTAS.glob("*.md"))


def nota_leer(nombre: str) -> str:
    p = NOTAS / f"{_safe_name(nombre)}.md"
    return p.read_text(encoding="utf-8") if p.exists() else f"No existe la nota '{nombre}'. Notas: {', '.join(notas_listar())}"


def nota_escribir(nombre: str, contenido: str, modo: str = "reemplazar") -> str:
    p = NOTAS / f"{_safe_name(nombre)}.md"
    if modo == "agregar" and p.exists():
        p.write_text(p.read_text(encoding="utf-8").rstrip("\n") + "\n" + contenido.strip() + "\n", encoding="utf-8")
    else:
        p.write_text(contenido.strip() + "\n", encoding="utf-8")
    return f"Nota '{p.stem}' guardada."


def nota_borrar(nombre: str) -> str:
    p = NOTAS / f"{_safe_name(nombre)}.md"
    if not p.exists():
        return "No existe."
    p.unlink()
    return f"Nota '{p.stem}' borrada."


# ---------- diario ----------

def diario_escribir(fecha: str, texto: str):
    (DIARIO / f"{fecha}.md").write_text(texto.strip() + "\n", encoding="utf-8")


def diario_reciente(n: int = 2) -> str:
    dias = sorted(DIARIO.glob("*.md"))[-n:]
    return "\n\n".join(f"### {p.stem}\n{p.read_text(encoding='utf-8')}" for p in dias)


# ---------- recordatorios ----------

def recordatorios() -> list[dict]:
    try:
        return json.loads(RECORDATORIOS.read_text(encoding="utf-8"))
    except Exception:
        return []


def _guardar_recordatorios(items: list[dict]):
    RECORDATORIOS.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def recordatorio_crear(cuando: str, texto: str) -> dict:
    dt = datetime.strptime(cuando.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if dt < ahora():
        raise ValueError("Esa fecha/hora ya pasó.")
    item = {"id": uuid.uuid4().hex[:6], "cuando": dt.isoformat(), "texto": texto.strip()}
    items = recordatorios() + [item]
    _guardar_recordatorios(sorted(items, key=lambda r: r["cuando"]))
    return item


def recordatorio_borrar(rid: str) -> bool:
    items = recordatorios()
    nuevos = [r for r in items if r["id"] != rid]
    _guardar_recordatorios(nuevos)
    return len(nuevos) != len(items)


def contexto(nucleo: str, conteo: dict[str, int]) -> str:
    """Bloque que se inyecta en el system prompt en cada mensaje. La memoria completa vive en SQLite
    (memoria.py); aquí solo va el NÚCLEO y cuántos datos más hay por categoría."""
    pend = recordatorios()[:15]
    rec = "\n".join(f"- [{r['id']}] {r['cuando'][:16].replace('T', ' ')}: {r['texto']}" for r in pend) or "(ninguno)"
    resto = ", ".join(f"{c}: {n}" for c, n in conteo.items()) or "(vacía)"
    return f"""=== TU MEMORIA — NÚCLEO (lo más importante; [#id] sirve para corregir con `recordar`) ===
{nucleo or '(vacío)'}

=== EN TU MEMORIA DE LARGO PLAZO HAY MÁS (búscalo con `recuerdos_buscar`) ===
Datos por categoría: {resto}

=== TUS NOTAS ===
{', '.join(notas_listar()) or '(ninguna)'}

=== RECORDATORIOS PENDIENTES ===
{rec}

=== TU DIARIO RECIENTE ===
{diario_reciente() or '(vacío)'}"""


TOOLS = [
    {"type": "function", "function": {
        "name": "nota_escribir",
        "description": "Crea o actualiza una nota en tu libreta (listas, planes, ideas, compras, pelis, lo que sea).",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"},
            "contenido": {"type": "string"},
            "modo": {"type": "string", "enum": ["reemplazar", "agregar"]}},
            "required": ["nombre", "contenido"]}}},
    {"type": "function", "function": {
        "name": "nota_leer",
        "description": "Lee una nota de tu libreta.",
        "parameters": {"type": "object", "properties": {"nombre": {"type": "string"}}, "required": ["nombre"]}}},
    {"type": "function", "function": {
        "name": "nota_borrar",
        "description": "Borra una nota que ya no sirve.",
        "parameters": {"type": "object", "properties": {"nombre": {"type": "string"}}, "required": ["nombre"]}}},
    {"type": "function", "function": {
        "name": "recordatorio_crear",
        "description": "Programa un mensaje para Chava a una hora exacta (zona horaria de Guadalajara).",
        "parameters": {"type": "object", "properties": {
            "cuando": {"type": "string", "description": "Formato 'AAAA-MM-DD HH:MM', 24 h."},
            "texto": {"type": "string", "description": "Qué recordarle."}},
            "required": ["cuando", "texto"]}}},
    {"type": "function", "function": {
        "name": "recordatorio_borrar",
        "description": "Cancela un recordatorio por su id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
]
