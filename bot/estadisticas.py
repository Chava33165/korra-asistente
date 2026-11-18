"""
Estadísticas de KORRA para la pestaña "KORRA" del homelab-dashboard.

Aquí vive todo lo de medir uso:
  - registrar(): guarda tokens y dinero de cada llamada a DeepSeek (tabla uso_llm, migración m005).
  - generar(): junta mensajes, DeepSeek, trabajos de Claude (GET /historial del worker), memoria y tareas
    en un resumen agregado (sin texto de pláticas).
  - escribir(): deja ese resumen en ESTADISTICAS_DIR/estadisticas.json; el dashboard lo monta en solo lectura.
"""
import json
import logging
import os
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("bot")
TZ = ZoneInfo(os.environ.get("TZ", "America/Mexico_City"))
ARCHIVO = Path(os.environ.get("ESTADISTICAS_DIR", "/estadisticas")) / "estadisticas.json"

# USD por millón de tokens: (entrada con caché, entrada sin caché, salida), (horario normal, horario pico).
# Fuente: https://api-docs.deepseek.com/quick_start/pricing (revisado 2026-10-14). "deepseek-chat" lo
# atiende DeepSeek-V4.1-Flash. Si DeepSeek cambia precios, solo se actualiza esta tabla.
PRECIOS_DEEPSEEK = {
    "flash": ((0.003, 0.15, 0.6), (0.006, 0.3, 1.2)),
    "pro": ((0.022, 0.66, 1.98), (0.044, 1.32, 3.96)),
}


def _es_pico(ts: float) -> bool:
    """Horario pico de DeepSeek: 01–04 y 06–10 UTC, de lunes a viernes."""
    u = datetime.fromtimestamp(ts, timezone.utc)
    return u.weekday() < 5 and (1 <= u.hour < 4 or 6 <= u.hour < 10)


def costo_deepseek(modelo: str, entrada: int, cache: int, salida: int, ts: float) -> float:
    tarifa = PRECIOS_DEEPSEEK["pro" if "pro" in (modelo or "") else "flash"][_es_pico(ts)]
    return (cache * tarifa[0] + entrada * tarifa[1] + salida * tarifa[2]) / 1_000_000


def registrar(db: sqlite3.Connection, origen: str, resp) -> None:
    """Guarda el uso de una respuesta de DeepSeek. Nunca rompe la respuesta si algo falla."""
    try:
        u = resp.usage
        if not u:
            return
        cache = getattr(u, "prompt_cache_hit_tokens", None) or 0
        entrada = getattr(u, "prompt_cache_miss_tokens", None)
        if entrada is None:
            entrada = max((u.prompt_tokens or 0) - cache, 0)
        salida = u.completion_tokens or 0
        ts = time.time()
        db.execute("INSERT INTO uso_llm(ts, proveedor, modelo, origen, entrada, entrada_cache, salida, costo_usd) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (ts, "deepseek", resp.model, origen, entrada, cache, salida,
                    costo_deepseek(resp.model, entrada, cache, salida, ts)))
        db.commit()
    except Exception:
        log.exception("registrar uso de DeepSeek")


def registrar_herramienta(db: sqlite3.Connection, nombre: str) -> None:
    try:
        db.execute("INSERT INTO uso_herramientas(ts, nombre) VALUES (?,?)", (time.time(), nombre))
        db.commit()
    except Exception:
        log.exception("registrar herramienta")


# ---------------------------------------------------------------- resumen

def _dia(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def _rachas(dias_activos: set[str], hoy: date) -> tuple[int, int]:
    """(racha actual, racha máxima) en días seguidos con mensajes. Hoy sin mensajes todavía no la rompe."""
    maxima = actual = 0
    previo = None
    for d in sorted(dias_activos):
        f = date.fromisoformat(d)
        actual = actual + 1 if previo and f - previo == timedelta(days=1) else 1
        maxima = max(maxima, actual)
        previo = f
    if not previo or (hoy - previo).days > 1:
        actual = 0
    return actual, maxima


def generar(db: sqlite3.Connection, claude: list[dict], saldo_deepseek: dict | None) -> dict:
    ahora = time.time()
    hoy = datetime.now(TZ).date()
    desde = ahora - 371 * 86400
    vacio_dia = lambda: {"mensajes": 0, "tokens_deepseek": 0, "tokens_claude": 0,
                         "costo_deepseek": 0.0, "costo_claude": 0.0, "trabajos": 0}
    dias: dict[str, dict] = defaultdict(vacio_dia)
    meses: dict[str, dict] = defaultdict(lambda: {"mensajes": 0,
        "deepseek": {"entrada": 0, "cache": 0, "salida": 0, "costo": 0.0, "llamadas": 0},
        "claude": {"entrada": 0, "cache": 0, "salida": 0, "costo_equiv": 0.0, "trabajos": 0}})
    horas = [0] * 24
    semana = [0] * 7  # lunes = 0
    modelos: Counter = Counter()

    # Mensajes de Chava (los de KORRA se cuentan aparte, como "respuestas").
    respuestas = 0
    primer_msg = None
    for rol, ts in db.execute("SELECT role, ts FROM msgs"):
        if rol != "user":
            respuestas += 1
            continue
        primer_msg = ts if primer_msg is None else min(primer_msg, ts)
        local = datetime.fromtimestamp(ts, TZ)
        horas[local.hour] += 1
        semana[local.weekday()] += 1
        meses[local.strftime("%Y-%m")]["mensajes"] += 1
        if ts >= desde:
            dias[local.strftime("%Y-%m-%d")]["mensajes"] += 1

    # DeepSeek (se mide desde la migración m005).
    primer_ds = None
    for ts, modelo, entrada, cache, salida, costo in db.execute(
            "SELECT ts, modelo, entrada, entrada_cache, salida, costo_usd FROM uso_llm WHERE proveedor='deepseek'"):
        primer_ds = ts if primer_ds is None else min(primer_ds, ts)
        total = entrada + cache + salida
        m = meses[datetime.fromtimestamp(ts, TZ).strftime("%Y-%m")]["deepseek"]
        m["entrada"] += entrada
        m["cache"] += cache
        m["salida"] += salida
        m["costo"] += costo
        m["llamadas"] += 1
        modelos[modelo or "deepseek"] += total
        if ts >= desde:
            d = dias[_dia(ts)]
            d["tokens_deepseek"] += total
            d["costo_deepseek"] += costo

    # Claude (trabajos del worker; el costo es "equivalente": va dentro del plan Pro, no se cobra aparte).
    trabajos_error = 0
    for j in claude:
        ts = j["ts"]
        trabajos_error += j.get("estado") == "error"
        m = meses[datetime.fromtimestamp(ts, TZ).strftime("%Y-%m")]["claude"]
        m["trabajos"] += 1
        m["costo_equiv"] += j.get("costo_usd") or 0
        total = 0
        for modelo, u in (j.get("modelos") or {}).items():
            cache = u.get("cache_lectura", 0) + u.get("cache_escritura", 0)
            m["entrada"] += u.get("entrada", 0)
            m["cache"] += cache
            m["salida"] += u.get("salida", 0)
            t = u.get("entrada", 0) + cache + u.get("salida", 0)
            modelos[modelo] += t
            total += t
        if ts >= desde:
            d = dias[_dia(ts)]
            d["tokens_claude"] += total
            d["costo_claude"] += j.get("costo_usd") or 0
            d["trabajos"] += 1

    herramientas = db.execute(
        "SELECT nombre, count(*) FROM uso_herramientas GROUP BY nombre ORDER BY 2 DESC LIMIT 8").fetchall()
    recuerdos = db.execute("SELECT count(*) FROM recuerdos WHERE estado='activo'").fetchone()[0]
    tareas = dict(db.execute("SELECT estado, count(*) FROM tareas GROUP BY estado").fetchall())

    activos = {d for d, v in dias.items() if v["mensajes"]}
    racha, racha_max = _rachas(activos, hoy)
    record = max(dias.items(), key=lambda kv: kv[1]["mensajes"], default=(None, {"mensajes": 0}))
    meses_orden = sorted(meses)[-12:]
    for v in dias.values():
        v["costo_deepseek"] = round(v["costo_deepseek"], 6)
        v["costo_claude"] = round(v["costo_claude"], 4)
    for k in meses_orden:
        meses[k]["deepseek"]["costo"] = round(meses[k]["deepseek"]["costo"], 6)
        meses[k]["claude"]["costo_equiv"] = round(meses[k]["claude"]["costo_equiv"], 4)

    mensajes = sum(semana)
    tokens_ds = sum(v["deepseek"]["entrada"] + v["deepseek"]["cache"] + v["deepseek"]["salida"] for v in meses.values())
    llamadas_ds = sum(v["deepseek"]["llamadas"] for v in meses.values())
    return {
        "version": 1,
        "generado": ahora,
        "hoy": hoy.isoformat(),
        "dias": dict(sorted(dias.items())),
        "meses": [{"mes": k, **meses[k]} for k in meses_orden],
        "horas": horas,
        "dias_semana": semana,
        "herramientas": [{"nombre": n, "veces": c} for n, c in herramientas],
        "modelos": [{"modelo": m, "tokens": t} for m, t in modelos.most_common(6)],
        "totales": {
            "mensajes": mensajes,
            "respuestas": respuestas,
            "dias_activos": len(activos),
            "racha_actual": racha,
            "racha_max": racha_max,
            "dia_record": {"fecha": record[0], "mensajes": record[1]["mensajes"]},
            "recuerdos": recuerdos,
            "tareas_hechas": tareas.get("hecha", 0),
            "tareas_pendientes": tareas.get("pendiente", 0),
            "trabajos_claude": len(claude),
            "trabajos_claude_error": trabajos_error,
            "costo_claude_equiv": round(sum(j.get("costo_usd") or 0 for j in claude), 2),
            "costo_deepseek": round(sum(v["deepseek"]["costo"] for v in meses.values()), 4),
            "tokens_por_respuesta_deepseek": round(tokens_ds / llamadas_ds) if llamadas_ds else 0,
            "desde": _dia(primer_msg) if primer_msg else None,
            "deepseek_desde": _dia(primer_ds) if primer_ds else None,
        },
        "saldo_deepseek": saldo_deepseek,
    }


def escribir(resumen: dict) -> None:
    """Escritura atómica (el dashboard nunca ve un archivo a medias) y legible por otros usuarios."""
    ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARCHIVO.with_suffix(".tmp")
    tmp.write_text(json.dumps(resumen, ensure_ascii=False, separators=(",", ":")))
    tmp.chmod(0o644)
    os.replace(tmp, ARCHIVO)
