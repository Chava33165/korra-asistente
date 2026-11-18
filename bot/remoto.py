"""Sesiones de Claude Code con Remote Control, para que Chava las use desde la app de Claude en su iPhone.

El bot no puede arrancar procesos en la Pi: deja la solicitud en el buzón de ~/Servicios/claude-remoto (montado en
/remoto/buzon) y el lanzador del host la atiende. Las carpetas viajan relativas al home de la Pi, como se ven en
Samba (Documentos/Escuela/…, KORRA, Servicios/…); el lanzador vuelve a validarlas.
"""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

BUZON = Path(os.environ.get("REMOTO_BUZON", "/remoto/buzon"))
ESTADO = Path(os.environ.get("REMOTO_ESTADO", "/remoto/estado.json"))
ESPERA_S = 60

# Ruta dentro del contenedor o nombre interno → ruta relativa al home de la Pi. El orden importa.
A_HOME = (("/jarvis/archivos", "Documentos/Personal"), ("/jarvis", "KORRA"), ("/workspace", "Documentos/Escuela"),
          ("/creaciones", "Documentos/Creaciones"), ("JARVIS/archivos", "Documentos/Personal"), ("JARVIS", "KORRA"),
          ("CETI COLOMOS", "Documentos/Escuela"), ("Creaciones", "Documentos/Creaciones"))
ATAJOS = {"home": "", "pi": "", "todo": "", "~": "", "servidor": "", "servicios": "Servicios", "korra": "KORRA",
          "personal": "Documentos/Personal", "creaciones": "Documentos/Creaciones", "escuela": "Documentos/Escuela",
          "documentos": "Documentos", "asistente": "Servicios/asistente", "dashboard": "Servicios/homelab-dashboard",
          "dinero": "Servicios/task-hub", "task-hub": "Servicios/task-hub"}


def a_home(texto: str) -> str:
    """'CETI COLOMOS/…', '/workspace/…', 'Documentos/Escuela/…', 'servicios'… → relativa al home ('' = el home)."""
    t = (texto or "").strip().strip("`'\"").rstrip("/")
    if t.lower() in ATAJOS:
        return ATAJOS[t.lower()]
    t = t.removeprefix("~/").removeprefix("/home/usuario/")
    for interno, home in A_HOME:
        if t == interno or t.startswith(interno + "/"):
            return home + t[len(interno):]
    return t.lstrip("/")


async def pedir(accion: str, carpeta: str = "", nombre: str = "") -> dict:
    """Manda una solicitud al lanzador y espera su respuesta."""
    if not BUZON.is_dir():
        return {"ok": False, "error": "No veo el buzón del lanzador (¿está montado ~/Servicios/claude-remoto?)."}
    rid = uuid.uuid4().hex[:10]
    tmp = BUZON / f"{rid}.tmp"
    tmp.write_text(json.dumps({"accion": accion, "carpeta": carpeta, "nombre": nombre}, ensure_ascii=False))
    tmp.replace(BUZON / f"{rid}.json")
    resp = BUZON / f"{rid}.resp.json"
    limite = time.time() + ESPERA_S
    while time.time() < limite:
        if resp.exists():
            datos = json.loads(resp.read_text())
            resp.unlink(missing_ok=True)
            return datos
        await asyncio.sleep(1)
    (BUZON / f"{rid}.json").unlink(missing_ok=True)
    return {"ok": False, "error": "El lanzador no contestó: en la Pi corre "
                                  "`python3 ~/Servicios/claude-remoto/lanzador.py &` (y revisa que esté en el crontab)."}


def lanzador_vivo() -> bool:
    """El lanzador renueva estado.json cada 30 s."""
    try:
        return time.time() - ESTADO.stat().st_mtime < 120
    except OSError:
        return False


def texto_sesion(s: dict, nueva: bool = True) -> str:
    carpeta = s["carpeta"].removeprefix("/home/usuario").lstrip("/") or "~ (toda la Pi)"
    cabeza = (f"🟢 Listo: abre la app de Claude → <b>Code</b> → «{s['nombre']}»" if nueva
              else f"🟢 «{s['nombre']}»")
    return cabeza + f"\n📁 {carpeta}" + (f"\n{s['url']}" if s.get("url") else "")


TOOLS = [{"type": "function", "function": {
    "name": "sesion_claude",
    "description": ("Abre, cierra o lista sesiones de Claude Code en la Pi que Chava maneja ÉL MISMO desde la app de Claude "
                    "en su iPhone (Remote Control). Úsala cuando pida 'ábreme Claude en…', 'quiero trabajar yo en la "
                    "carpeta de…', 'sesión de Claude Code'. NO es para encargarle trabajos (eso es delegar_a_claude). "
                    "Máximo 2 abiertas."),
    "parameters": {"type": "object", "properties": {
        "accion": {"type": "string", "enum": ["abrir", "cerrar", "listar"]},
        "materia": {"type": "string", "description": "Si es de una materia (abre su carpeta del parcial)."},
        "parcial": {"type": "string", "description": "P1/P2/P3; vacío = el parcial actual."},
        "carpeta": {"type": "string", "description": ("Si no es una materia: una ruta como se ve en Samba "
                                                      "('Documentos/Creaciones/Videos', 'Servicios/asistente') o un atajo: "
                                                      "home, servicios, korra, personal, creaciones, escuela, asistente, dinero.")},
        "nombre": {"type": "string", "description": "Para cerrar: el nombre de la sesión. Para abrir: opcional."}},
        "required": ["accion"]}}}]
