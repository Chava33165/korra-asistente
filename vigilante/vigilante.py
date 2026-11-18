#!/usr/bin/env python3
"""Vigilante del servidor: lo corre cron cada 10 minutos en el host (no en un contenedor).

Revisa contenedores, disco, temperatura, swap y que el respaldo de iCloud esté al día. Avisa por Telegram
(avisar.sh) solo cuando aparece un problema nuevo o cuando uno ya se resolvió, y deja el resultado en
estado.json, que KORRA lee para /servidor y el resumen de la mañana.

Prueba: VIGILANTE_PRUEBA=1 baja el umbral del disco a 1 % para forzar un aviso.
Solo usa la biblioteca estándar: el host no tiene paquetes de Python.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

AQUI = Path(__file__).resolve().parent
ESTADO = AQUI / "estado.json"
AVISAR = AQUI.parent / "avisar.sh"
ICLOUD = AQUI.parent.parent / "icloud-respaldo" / "estado" / "icloud.json"
LANZADOR = AQUI.parent.parent / "claude-remoto" / "estado.json"   # su latido se renueva cada 30 s

DISCO_MAX = 1 if os.environ.get("VIGILANTE_PRUEBA") else 85   # % usado de /
TEMP_MAX = 75        # °C
SWAP_MAX = 80        # % de swap usada
RESPALDO_MAX = timedelta(hours=26)


def contenedores() -> dict[str, dict]:
    salida = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
        capture_output=True, text=True, timeout=60, check=True).stdout
    res = {}
    for linea in salida.splitlines():
        nombre, estado, status = linea.split("\t")
        res[nombre] = {"estado": estado, "unhealthy": "(unhealthy)" in status, "reinicio": ""}
    if res:  # política de reinicio: con unless-stopped/always Docker los levanta solo si truenan
        pol = subprocess.run(["docker", "inspect", "-f", "{{.Name}}\t{{.HostConfig.RestartPolicy.Name}}", *res],
                             capture_output=True, text=True, timeout=60).stdout
        for linea in pol.splitlines():
            nombre, _, politica = linea.lstrip("/").partition("\t")
            if nombre in res:
                res[nombre]["reinicio"] = politica
    return res


def metricas() -> dict:
    st = os.statvfs("/")
    disco = round(100 * (1 - st.f_bavail / st.f_blocks), 1)
    temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    mem = {k: int(v.split()[0]) for k, v in
           (l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())}
    swap = round(100 * (1 - mem["SwapFree"] / mem["SwapTotal"]), 1) if mem["SwapTotal"] else 0
    return {"disco_pct": disco, "temp_c": temp, "swap_pct": swap,
            "ram_libre_mb": mem["MemAvailable"] // 1024}


def problemas(cont: dict, antes: dict, m: dict) -> dict[str, str]:
    p = {}
    corrian = set(antes.get("corriendo", []))
    for nombre, c in cont.items():
        if c["estado"] == "running" and c["unhealthy"]:
            p[f"unhealthy:{nombre}"] = f"🩺 {nombre} está corriendo pero falla su revisión de salud"
        elif c["estado"] == "restarting":
            p[f"caido:{nombre}"] = f"🔁 {nombre} se está reiniciando una y otra vez"
        elif c["estado"] != "running" and c["reinicio"] not in ("unless-stopped", "always") \
                and (nombre in corrian or f"caido:{nombre}" in antes.get("problemas", {})):
            # Solo los que estaban prendidos y que Docker no reinicia solo: si tienen unless-stopped/always y están
            # apagados, alguien los apagó a propósito (si truenan, Docker los levanta y salen "restarting").
            p[f"caido:{nombre}"] = f"🔴 Se cayó {nombre} ({c['estado']})"
    if m["disco_pct"] > DISCO_MAX:
        p["disco"] = f"💾 Disco al {m['disco_pct']} %"
    if m["temp_c"] > TEMP_MAX:
        p["temp"] = f"🌡️ La Pi está a {m['temp_c']:.0f} °C"
    if m["swap_pct"] > SWAP_MAX:
        p["swap"] = f"🧠 Swap al {m['swap_pct']} % (RAM libre: {m['ram_libre_mb']} MB)"
    try:
        icloud = json.loads(ICLOUD.read_text())
        ultimo = icloud.get("ultimo_ok")
        if not ultimo or datetime.now().astimezone() - datetime.fromisoformat(ultimo) > RESPALDO_MAX:
            p["respaldo"] = f"☁️ El respaldo de iCloud no ha salido bien desde {ultimo or 'nunca'}"
    except (OSError, ValueError):
        p["respaldo"] = "☁️ No encuentro el estado del respaldo de iCloud"
    try:
        if time.time() - LANZADOR.stat().st_mtime > 600:
            p["lanzador"] = "💻 El lanzador de sesiones de Claude Code (/pc) está apagado"
    except OSError:
        p["lanzador"] = "💻 El lanzador de sesiones de Claude Code (/pc) nunca ha arrancado"
    return p


def avisar(texto: str) -> bool:
    try:
        return subprocess.run([str(AVISAR), "--texto", texto], capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    try:
        antes = json.loads(ESTADO.read_text())
    except (OSError, ValueError):
        antes = {}
    cont, m = contenedores(), metricas()
    ahora = problemas(cont, antes, m)
    previos = antes.get("problemas", {})

    nuevos = [t for k, t in ahora.items() if k not in previos]
    resueltos = [t for k, t in previos.items() if k not in ahora]
    avisados = previos
    if nuevos or resueltos:
        partes = (["⚠️ Vigilante del servidor:"] + nuevos if nuevos else []) + \
                 (["✅ Ya se resolvió:"] + resueltos if resueltos else [])
        if avisar("\n".join(partes)):
            avisados = ahora   # si no se pudo avisar, se reintenta en la siguiente vuelta

    estado = {
        "revisado": datetime.now().astimezone().isoformat(timespec="seconds"),
        "metricas": m,
        "problemas": avisados,   # lo que Chava ya sabe
        "problemas_actuales": ahora,
        "corriendo": sorted(n for n, c in cont.items() if c["estado"] == "running"),
    }
    tmp = ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1))
    tmp.replace(ESTADO)


if __name__ == "__main__":
    main()
