"""Estado del respaldo de iCloud y del vigilante del servidor.

Los dos los escriben scripts del host (Servicios/icloud-respaldo/respaldo.sh y Servicios/asistente/vigilante/
vigilante.py) y llegan aquí montados de solo lectura. KORRA solo los lee: para /respaldos, /servidor y el
resumen de la mañana.
"""
import json
import os
from datetime import datetime
from pathlib import Path

ICLOUD = Path(os.environ.get("ESTADO_ICLOUD", "/estado/icloud/icloud.json"))
VIGILANTE = Path(os.environ.get("ESTADO_VIGILANTE", "/estado/vigilante/estado.json"))


def _leer(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _hace(iso: str | None) -> str:
    if not iso:
        return "nunca"
    seg = (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds()
    if seg < 3600:
        return f"hace {int(seg // 60)} min"
    if seg < 86400 * 2:
        return f"hace {seg / 3600:.0f} h"
    return f"hace {seg / 86400:.0f} días"


def _gb(b: int) -> str:
    return f"{b / 1e9:.1f} GB"


def icloud() -> dict | None:
    return _leer(ICLOUD)


def vigilante() -> dict | None:
    return _leer(VIGILANTE)


def texto_respaldos() -> str:
    """Mensaje de /respaldos (HTML de Telegram)."""
    e = icloud()
    if not e:
        return "☁️ Todavía no hay registro del respaldo de iCloud (corre cada día a las 2 AM)."
    icono = {"ok": "✅", "sesion_vencida": "🔑", "error": "⚠️"}.get(e["resultado"], "❔")
    lineas = [
        "<b>☁️ Respaldo de iCloud</b>",
        f"{icono} Última corrida: {_hace(e['fin'])} ({e['fin'][:16].replace('T', ' ')}, {e['duracion_s'] // 60} min)",
        f"Último respaldo bueno: {_hace(e.get('ultimo_ok'))}",
        f"Copiados esa vez: {e['copiados']} · a Scrap: {e['a_scrap']}",
        f"Tamaño: {_gb(e['bytes_icloud'])} en iCloud · {_gb(e['bytes_scrap'])} en Scrap (versiones viejas)",
    ]
    if e["resultado"] == "sesion_vencida":
        lineas.append("\n🔑 Apple pide iniciar sesión: en la Pi corre <code>~/Servicios/icloud-respaldo/conectar.sh</code>")
    elif e["resultado"] == "error" and e.get("errores"):
        lineas.append("\nÚltimos errores:\n<code>" + "\n".join(x[:200] for x in e["errores"][-3:]) + "</code>")
    lineas.append("\nSe ven desde la Mac en Samba → Respaldos.")
    return "\n".join(lineas)


def lineas_vigilante() -> list[str]:
    """Para /servidor: qué ve el vigilante ahora mismo."""
    v = vigilante()
    if not v:
        return ["❔ Vigilante sin datos (cron cada 10 min)"]
    problemas = list(v.get("problemas_actuales", {}).values())
    cabeza = f"🛡 Vigilante ({_hace(v['revisado'])}): " + ("todo bien" if not problemas else f"{len(problemas)} problema(s)")
    return [cabeza] + problemas


def resumen_para_prompt() -> str:
    """Estado corto para el resumen de la mañana: solo lo que vale la pena contar."""
    partes = []
    e = icloud()
    if not e:
        partes.append("Respaldo de iCloud: sin registro.")
    elif e["resultado"] == "ok":
        partes.append(f"Respaldo de iCloud: bien, {_hace(e['fin'])}, {e['copiados']} archivos nuevos o cambiados.")
    else:
        partes.append(f"Respaldo de iCloud: FALLÓ ({e['resultado']}); el último bueno fue {_hace(e.get('ultimo_ok'))}.")
    v = vigilante()
    if v:
        problemas = list(v.get("problemas_actuales", {}).values())
        m = v.get("metricas", {})
        partes.append("Servidor: " + ("; ".join(problemas) if problemas else
                      f"todo bien (disco {m.get('disco_pct')} %, {m.get('temp_c', 0):.0f} °C)."))
    return " ".join(partes)
