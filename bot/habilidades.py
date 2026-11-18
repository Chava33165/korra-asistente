"""
Habilidades de KORRA: manuales cortos en Markdown que carga solo cuando los necesita (como las skills de Claude).

En cada mensaje KORRA ve solo el ÍNDICE (nombre + cuándo usarla); el cuerpo lo abre con la herramienta `habilidad`.
Así el prompt fijo se queda corto y cada manual crece sin estorbar a los demás.

Dos lugares:
  bot/habilidades/*.md        → de fábrica (vienen con el código)
  JARVIS/habilidades/*.md     → las que KORRA o Chava crean o ajustan (se ven por Samba en KORRA/habilidades);
                                si tienen el mismo nombre que una de fábrica, la reemplazan.

Formato de cada archivo:
    ---
    nombre: calendario
    cuando: agendar, mover o borrar eventos; "¿qué tengo?", "¿estoy libre?"; horario de clases
    ---
    (instrucciones)
"""
import re
from pathlib import Path

import jarvis

FABRICA = Path(__file__).with_name("habilidades")
PROPIAS = jarvis.BASE / "habilidades"


def _leer(p: Path) -> dict | None:
    texto = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", texto, re.S)
    if not m:
        return None
    meta = dict(re.findall(r"^(\w+):\s*(.+)$", m[1], re.M))
    if not meta.get("nombre") or not meta.get("cuando"):
        return None
    return {"nombre": meta["nombre"].strip(), "cuando": meta["cuando"].strip(), "cuerpo": m[2].strip(),
            "origen": "propia" if p.is_relative_to(PROPIAS) else "fábrica"}


def todas() -> dict[str, dict]:
    res = {}
    for carpeta in (FABRICA, PROPIAS):  # las propias van después: reemplazan a las de fábrica
        for p in sorted(carpeta.glob("*.md")) if carpeta.is_dir() else []:
            if h := _leer(p):
                res[h["nombre"]] = h
    return res


def indice() -> str:
    """Lo que va en el prompt de cada mensaje: una línea por habilidad."""
    return "\n".join(f"- `{h['nombre']}` → {h['cuando']}" for h in todas().values())


def abrir(nombre: str) -> str:
    hs = todas()
    h = hs.get(nombre.strip().lower())
    if not h:
        return f"No existe la habilidad '{nombre}'. Hay: {', '.join(hs)}"
    return f"# Habilidad: {h['nombre']}\n{h['cuerpo']}"


def escribir(nombre: str, cuando: str, instrucciones: str) -> str:
    nombre = re.sub(r"[^a-z0-9\-]", "-", nombre.strip().lower()).strip("-")[:40]
    if not nombre or len(instrucciones.strip()) < 40:
        return "Rechazado: falta nombre o las instrucciones están casi vacías."
    PROPIAS.mkdir(parents=True, exist_ok=True)
    p = PROPIAS / f"{nombre}.md"
    if p.exists():  # guarda la versión anterior por si la nueva sale peor
        p.with_suffix(".md.bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(f"---\nnombre: {nombre}\ncuando: {cuando.strip()}\n---\n{instrucciones.strip()}\n", encoding="utf-8")
    return f"Habilidad '{nombre}' guardada en KORRA/habilidades (Chava la ve por Samba)."


TOOLS = [
    {"type": "function", "function": {
        "name": "habilidad",
        "description": ("Abre el manual de una de tus habilidades (la lista está en tu prompt). Ábrela ANTES de hacer algo "
                        "de ese tipo; puedes abrir varias."),
        "parameters": {"type": "object", "properties": {"nombre": {"type": "string"}}, "required": ["nombre"]}}},
    {"type": "function", "function": {
        "name": "habilidad_escribir",
        "description": ("Crea o mejora una habilidad PROPIA cuando Chava te enseña cómo quiere que hagas algo que se va a repetir "
                        "(\"cuando te pida X, hazlo así\") o te lo pide. Escribe pasos concretos en segunda persona, con el "
                        "resultado esperado de cada uno. Para ajustar una de fábrica, ábrela, cópiala y escríbela con el mismo nombre."),
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string", "description": "corto, minúsculas con guiones, p. ej. 'recibos-plexus'"},
            "cuando": {"type": "string", "description": "una línea: en qué casos abrirla (es lo que verás en tu índice)"},
            "instrucciones": {"type": "string"}},
            "required": ["nombre", "cuando", "instrucciones"]}}},
]


def ejecutar(nombre: str, args: dict) -> str | None:
    if nombre == "habilidad":
        return abrir(args.get("nombre", ""))
    if nombre == "habilidad_escribir":
        return escribir(args["nombre"], args["cuando"], args["instrucciones"])
    return None
