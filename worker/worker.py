"""
Worker: recibe trabajos del bot y los ejecuta con Claude Code en modo headless.

Solo es alcanzable dentro de la red de docker compose (no publica puertos).
Corre un trabajo a la vez porque la Raspberry Pi tiene 4 GB de RAM.

API:
  POST /jobs              {"prompt": str, "cwd": "SEMESTRE_1/MATERIA/P1", "timeout_s": 0, "escuela": bool} -> {"id": ...}
  GET  /jobs/{id}         estado + resultado + entregables (con su revisión) + archivos nuevos/modificados/borrados
  POST /comprimir         copia de un video que cabe en Telegram (< 45 MB), en /data/tmp (nunca en la carpeta)
  POST /jobs/{id}/cancel
  GET  /jobs              trabajos recientes
  POST /limpiar           suelta memoria, olvida trabajos viejos y mata procesos atorados
  GET  /historial         tokens y costo de cada trabajo terminado (sale de los logs; para estadísticas)
  GET  /sesiones          sesiones pasadas de Claude (se continúan con POST /jobs {"resume": id})
  GET  /skills            skills y plugins globales que ve Claude
  POST /skills            instala skills globales con `npx skills add … -g` (sin gastar Claude)
  GET  /health
"""
import asyncio
import ctypes
import gc
import json
import os
import re
import shlex
import shutil
import signal
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import revisar

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace")).resolve()
# Espacio personal de KORRA (~/KORRA). En las rutas relativas de la API se escribe "JARVIS/...".
JARVIS = Path(os.environ.get("JARVIS", "/jarvis")).resolve()
# Videos, diseños y presentaciones que no son de la escuela (~/Documentos/Creaciones). En la API: "Creaciones/...".
CREACIONES = Path(os.environ.get("CREACIONES", "/creaciones")).resolve()
RAICES = [(p, r) for p, r in (("JARVIS/", JARVIS), ("Creaciones/", CREACIONES)) if r.is_dir()] + [("", WORKSPACE)]
LOG_DIR = Path(os.environ.get("LOG_DIR", "/data/logs"))
ESTADO_JOBS = Path(os.environ.get("ESTADO_JOBS", "/data/jobs.json"))  # para que un reinicio no pierda resultados
PREVIAS = Path("/data/previas")
TMP = Path("/data/tmp")
# Carpetas que no se revisan para ver qué cambió: código de proyectos y dependencias (un Remotion trae ~20 mil archivos).
SALTAR = {"node_modules", "__pycache__", "dist", "build", "out", "_proyecto", ".remotion", ".git", ".korra"}
EXT_ENTREGABLE = {".docx", ".pdf", ".pptx", ".xlsx", ".mp4", ".mov", ".m4v", ".webm", ".png", ".jpg", ".jpeg",
                  ".webp", ".svg", ".gif", ".mp3", ".m4a", ".wav", ".zip", ".odt", ".txt", ".md", ".csv"}
TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "1200"))
MAX_TURNS = os.environ.get("CLAUDE_MAX_TURNS", "80")
MODEL = os.environ.get("CLAUDE_MODEL", "")  # vacío = el default de tu suscripción
HOME = Path.home()
PROYECTOS = HOME / ".claude" / "projects"   # ahí guarda Claude cada sesión: <carpeta>/<id>.jsonl
SKILLS = HOME / ".claude" / "skills"        # = ~/.claude/skills de la Pi (las mismas skills globales)
# ~/.claude/plugins de la Pi, montado en su MISMA ruta porque installed_plugins.json guarda rutas absolutas.
PLUGINS = Path(os.environ.get("PLUGINS_DIR", "/nonexistent"))

# Contexto extra que Claude recibe en cada trabajo (además del CLAUDE.md de la carpeta).
APPEND_SYSTEM = (
    "Estás corriendo sin supervisión, lanzado desde KORRA, el bot de Telegram de Salvador (\"Chava\"). "
    "No puedes hacer preguntas: si algo es ambiguo, toma la decisión más razonable y menciónala al final. "
    "Tu respuesta final se le envía por Telegram: en español, corta (máximo ~10 líneas): qué hiciste, qué decidiste "
    "por él y dónde quedó cada archivo. "
    "CARPETAS. Él las ve desde la Mac por Samba; escribe las rutas así, nunca la ruta interna: "
    "/workspace = 'Documentos/Escuela' (escuela: SEMESTRE_N/<MATERIA>/<Px>); "
    "/creaciones = 'Documentos/Creaciones' (videos, diseños y presentaciones que NO son de la escuela: "
    "Videos/, Diseños/, Presentaciones/, una carpeta '<AAAA-MM-DD> <título>' por proyecto); "
    "/jarvis/archivos = 'Documentos/Personal' (papeles personales: nóminas, recibos…); "
    "/jarvis = 'KORRA' (su asistente: no lo toques salvo que te lo pidan). "
    "Lo que manda por Telegram llega a /workspace/_TELEGRAM/entrada. "
    "Trabaja en la carpeta en la que te lanzaron (es la que eligió KORRA); no guardes en otra salvo que el pedido lo diga. "
    "ORDEN. Los archivos FINALES van arriba en esa carpeta; todo lo demás (código de Remotion/HyperFrames/HTML, "
    "assets, guiones, renders intermedios) va en '_proyecto/' dentro de ella. Al terminar un video o diseño que sí "
    "quedó, borra '_proyecto/**/node_modules' (se regenera con `npm install`): es lo ÚNICO que puedes borrar. "
    "Nunca sobrescribas un archivo que ya existía: usa ' v2', ' v3'. "
    "ENTREGA. Antes de responder escribe '.korra/entrega.json' en la carpeta de trabajo con "
    '{"archivos": ["ruta de cada archivo FINAL que Salvador debe recibir"], "resumen": "una línea", '
    '"pendiente": "lo que faltó, o vacío"} (rutas internas /workspace/…, /creaciones/… o relativas a la carpeta). '
    "Solo finales: nada de código, borradores ni copias de la entrada. "
    "REVISA tu trabajo antes de entregar: abre/convierte el archivo, cuenta páginas o diapositivas, y en video revisa "
    "duración y resolución con ffprobe y mira un par de fotogramas. "
    "Tu turno termina cuando respondes y entonces se mata todo lo que dejaste corriendo: NUNCA dejes un render, "
    "build o instalación en segundo plano ni digas que esperas un aviso; córrelo en primer plano y espera. Si no "
    "alcanza el tiempo, di exactamente qué falta y con qué comando se termina. "
    "Para video/capturas hay Chromium en /usr/bin/chromium (headless, con --no-sandbox) y ffmpeg. "
    "Tienes las skills y plugins globales de Salvador (~/.claude/skills): úsalas cuando apliquen. Si pide "
    "instalar skills, hazlo global: `npx -y skills add <paquete> -g -y -a claude-code`."
)

app = FastAPI()
jobs: dict[str, dict] = {}
queue: asyncio.Queue[str] = asyncio.Queue()
procs: dict[str, asyncio.subprocess.Process] = {}
pdf_activos = 0  # conversiones de LibreOffice e instalaciones de skills en curso (para no matarlas en /limpiar)


class JobIn(BaseModel):
    prompt: str
    cwd: str = ""
    # Opcionales, para trabajos automáticos que deben gastar poco del límite del plan:
    model: str = ""                 # p. ej. "claude-haiku-4-5" (vacío = CLAUDE_MODEL / default del plan)
    max_turns: int = 0              # 0 = CLAUDE_MAX_TURNS
    disallowed_tools: list[str] = []  # p. ej. ["Task", "Agent", "Skill"] para que no lance sub-agentes
    resume: str = ""                # id de una sesión pasada (GET /sesiones): la continúa en su misma carpeta
    timeout_s: int = 0              # 0 = JOB_TIMEOUT_S (los videos piden más)
    escuela: bool = False           # revisar como entrega escolar (portada, prefijo del registro)


def resolve_cwd(rel: str) -> Path:
    base = WORKSPACE
    for prefijo, raiz in RAICES:
        if prefijo and (rel == prefijo.rstrip("/") or rel.startswith(prefijo)):
            base, rel = raiz, rel[len(prefijo):]
            break
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise HTTPException(400, "cwd fuera de las carpetas permitidas")
    return p


def _rel(cwd: str) -> str | None:
    """Ruta absoluta del contenedor → ruta relativa de la API ("SEMESTRE_1/…" o "JARVIS/…")."""
    p = Path(cwd)
    for prefijo, raiz in RAICES:
        if p == raiz:
            return prefijo.rstrip("/")
        if raiz in p.parents:
            return prefijo + str(p.relative_to(raiz))
    return None


# Lo que el bot le antepone al pedido; se quita para que el título de la sesión sea lo que pidió Salvador.
_ENVOLTURA = re.compile(r"^(Pedido directo de Salvador por Telegram \(comando /claude\):|Carpeta de trabajo:[^\n]*)\s*")


def _leer_sesion(f: Path) -> dict | None:
    """Datos de una sesión a partir de su primer mensaje (solo lee el principio del archivo)."""
    try:
        with f.open(errors="replace") as fh:
            for i, linea in enumerate(fh):
                if i > 40:
                    break
                d = json.loads(linea)
                texto = (d.get("message") or {}).get("content") if d.get("type") == "user" else None
                if not isinstance(texto, str) or d.get("isSidechain"):
                    continue
                rel = _rel(d.get("cwd") or "")
                if rel is None:
                    return None
                titulo = next((l.strip() for l in _ENVOLTURA.sub("", texto).splitlines() if l.strip()), "(sin título)")
                return {"id": f.stem, "cwd": rel, "titulo": titulo[:80], "modificada": f.stat().st_mtime}
    except (OSError, ValueError):
        pass
    return None


def _buscar_sesion(sid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f-]{36}", sid):
        return None
    for f in PROYECTOS.glob(f"*/{sid}.jsonl"):
        return _leer_sesion(f)
    return None


def _plugins() -> list[Path]:
    """Carpetas de los plugins instalados en la Pi (se cargan con --plugin-dir en cada trabajo)."""
    try:
        inst = json.loads((PLUGINS / "installed_plugins.json").read_text())["plugins"]
    except (OSError, ValueError, KeyError):
        return []
    rutas = [Path(v["installPath"]) for versiones in inst.values() for v in versiones[-1:] if v.get("installPath")]
    return [r for r in rutas if r.is_dir()]


def snapshot(base: Path) -> dict[str, tuple[float, int]]:
    """Archivos de la carpeta del trabajo (no de todo el disco), sin código de proyecto ni dependencias."""
    snap = {}
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SALTAR]
        for f in files:
            if f.startswith((".", "~$")):
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            if (rel := _rel(full)) is not None:
                snap[rel] = (st.st_mtime, st.st_size)
    return snap


def _ruta_interna(texto: str, cwd: Path) -> Path:
    """Ruta que escribió Claude (interna, relativa a la carpeta o con el nombre de la API) → ruta real."""
    t = texto.strip()
    if t.startswith("/"):
        return Path(t).resolve()
    if any(t.startswith(pref) for pref, _ in RAICES if pref) or t.startswith("SEMESTRE_"):
        return resolve_cwd(t)
    return (cwd / t).resolve()


def entregables(job: dict, cwd: Path) -> list[str]:
    """Lo que Claude dijo que es el resultado (.korra/entrega.json) o, si no lo escribió, lo cambiado que parece final."""
    manifiesto = cwd / ".korra" / "entrega.json"
    try:
        if manifiesto.stat().st_mtime >= job["started"]:
            datos = json.loads(manifiesto.read_text())
            job["entrega"] = {k: datos.get(k, "") for k in ("resumen", "pendiente")}
            rutas = [_ruta_interna(str(a), cwd) for a in datos.get("archivos", [])]
            return [r for p in rutas if p.is_file() and (r := _rel(str(p))) is not None]
    except (OSError, ValueError, HTTPException):
        pass
    return [c for c in job["changed"] if Path(c).suffix.lower() in EXT_ENTREGABLE and "_TELEGRAM/" not in c
            and "/entrada/" not in c and not Path(c).name.lower().startswith(("readme", "guion", "storyboard"))]


def tipo_error(job: dict, data: dict, err: bytes) -> str:
    texto = (str(data.get("result", "")) + err.decode(errors="replace")[-2000:]).lower()
    if re.search(r"hit your .{0,20}limit|usage limit|rate limit|session limit", texto):
        return "limite"
    return {"timeout": "timeout", "cancelled": "cancelado"}.get(job["status"], "fallo")


def guardar_jobs():
    """Estado de los trabajos en disco: si el worker se reinicia, el bot igual recoge el resultado."""
    ESTADO_JOBS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO_JOBS.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False))
    tmp.replace(ESTADO_JOBS)


def cargar_jobs():
    try:
        jobs.update(json.loads(ESTADO_JOBS.read_text()))
    except (OSError, ValueError):
        return
    for j in jobs.values():
        if j["status"] in ("queued", "running"):  # se cortaron con el reinicio: no se relanzan solos (cuestan)
            j.update(status="error", error_tipo="interrumpido", finished=time.time(),
                     result="El worker se reinició a mitad del trabajo.")


async def run_job(job_id: str):
    job = jobs[job_id]
    cwd = resolve_cwd(job["cwd"])
    cwd.mkdir(parents=True, exist_ok=True)

    cmd = [
        "claude", "-p", job["prompt"],
        "--output-format", "json",
        # El contenedor es el sandbox: solo ve CETI COLOMOS y su propio home, corre sin
        # root y sin capabilities. Por eso se permite todo dentro de él.
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(job.get("max_turns") or MAX_TURNS),
        "--append-system-prompt", APPEND_SYSTEM,
        "--add-dir", str(WORKSPACE),
        *(["--add-dir", str(JARVIS)] if JARVIS.is_dir() else []),
        *(["--add-dir", str(CREACIONES)] if CREACIONES.is_dir() else []),
    ]
    for plugin in _plugins():
        cmd += ["--plugin-dir", str(plugin)]
    if job.get("resume"):
        cmd += ["--resume", job["resume"]]
    if job.get("model") or MODEL:
        cmd += ["--model", job.get("model") or MODEL]
    if job.get("disallowed_tools"):
        cmd += ["--disallowedTools", ",".join(job["disallowed_tools"])]

    before = await asyncio.to_thread(snapshot, cwd)
    job.update(status="running", started=time.time())
    guardar_jobs()

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    procs[job_id] = proc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=job.get("timeout_s") or TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        job["status"] = "timeout"
    finally:
        procs.pop(job_id, None)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{job_id}.log").write_bytes(
        b"== CMD cwd=" + str(cwd).encode() + b"\n" + job["prompt"].encode()
        + b"\n== STDOUT\n" + out + b"\n== STDERR\n" + err
    )

    try:
        data = json.loads(out.decode() or "{}")
    except json.JSONDecodeError:
        data = {}

    if job["status"] == "running":
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
        elif proc.returncode == 0 and not data.get("is_error"):
            job["status"] = "done"
        else:
            job["status"] = "error"

    job["result"] = data.get("result") or err.decode(errors="replace")[-1500:] or out.decode(errors="replace")[-1500:]
    job["cost_usd"] = data.get("total_cost_usd")
    job["session_id"] = data.get("session_id")
    job["num_turns"] = data.get("num_turns")

    after = await asyncio.to_thread(snapshot, cwd)
    job["changed"] = sorted(p for p, v in after.items() if before.get(p) != v)
    job["deleted"] = sorted(p for p in before if p not in after)
    job["entregables"] = entregables(job, cwd)
    if job["status"] != "done":
        job["error_tipo"] = tipo_error(job, data, err)
    elif job["entregables"]:
        job["revision"] = await asyncio.to_thread(revisar_entregables, job_id, job["entregables"], job.get("escuela"))
    job["finished"] = time.time()
    guardar_jobs()


def revisar_entregables(job_id: str, rutas: list[str], escuela: bool) -> dict:
    destino = PREVIAS / job_id
    destino.mkdir(parents=True, exist_ok=True)
    revision = {}
    for i, r in enumerate(rutas[:10]):
        res = revisar.revisar_archivo(resolve_cwd(r), destino / f"{i:02d}", escuela)
        res["previa"] = f"{job_id}_{i:02d}.jpg" if res["previa"] else ""  # se baja con GET /archivo/<nombre>
        revision[r] = res
    return revision


async def consumer():
    while True:
        job_id = await queue.get()
        if jobs[job_id]["status"] != "queued":
            continue
        try:
            await run_job(job_id)
        except Exception as e:  # que un trabajo roto no tumbe la cola
            jobs[job_id].update(status="error", error_tipo="fallo", result=f"Error interno del worker: {e}",
                                finished=time.time())
            guardar_jobs()


@app.on_event("startup")
async def startup():
    os.umask(0o002)
    cargar_jobs()
    asyncio.create_task(consumer())


class PdfIn(BaseModel):
    ruta: str  # relativa a CETI COLOMOS, o "JARVIS/..." para el espacio personal


@app.post("/pdf")
async def a_pdf(body: PdfIn):
    """Convierte docx/pptx/xlsx/odt… a PDF con LibreOffice (para imprimir). No usa Claude.
    Deja el PDF en CETI COLOMOS/_TELEGRAM/impresion/ y devuelve su ruta relativa."""
    origen = resolve_cwd(body.ruta)
    if not origen.is_file():
        raise HTTPException(404, "no existe ese archivo")
    salida = WORKSPACE / "_TELEGRAM" / "impresion"
    salida.mkdir(parents=True, exist_ok=True)
    global pdf_activos
    pdf_activos += 1
    try:
        proc = await asyncio.create_subprocess_exec(
            "soffice", "--headless", "--convert-to", "pdf", "--outdir", str(salida), str(origen),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "LibreOffice tardó demasiado")
    finally:
        pdf_activos -= 1
    pdf = salida / (origen.stem + ".pdf")
    if not pdf.is_file():
        raise HTTPException(500, (err or out).decode(errors="replace")[-300:] or "no se generó el PDF")
    return {"pdf": str(pdf.relative_to(WORKSPACE))}


@app.get("/uso")
async def uso():
    """Uso del plan de Claude (Pro/Max). Se lee de los encabezados de rate limit de una petición
    mínima a Haiku (1 token), porque el token de `claude setup-token` no puede leer /api/oauth/usage."""
    import urllib.request
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                       "system": "You are Claude Code, Anthropic's official CLI for Claude.",
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "authorization": f"Bearer {os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}",
        "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20", "content-type": "application/json"})
    try:
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=20)
        h = resp.headers
    except urllib.error.HTTPError as e:  # 429 = límite alcanzado; los encabezados siguen llegando
        h = e.headers
    pref = "anthropic-ratelimit-unified-"
    return {k[len(pref):]: v for k, v in h.items() if k.lower().startswith(pref)}


def _rss_mb() -> float:
    for linea in Path("/proc/self/status").read_text().splitlines():
        if linea.startswith("VmRSS:"):
            return int(linea.split()[1]) / 1024
    return 0.0


MAX_JOBS = 20  # trabajos terminados que se conservan (los que muestra GET /jobs)


@app.post("/limpiar")
async def limpiar():
    """Suelta memoria: olvida trabajos terminados viejos, mata procesos que se quedaron atorados
    (LibreOffice o hijos de Claude que sobrevivieron a su trabajo) y le devuelve RAM al sistema."""
    antes = _rss_mb()
    terminados = sorted((j for j in jobs.values() if j["status"] not in ("queued", "running")),
                        key=lambda j: j["created"], reverse=True)
    viejos = [j["id"] for j in terminados[MAX_JOBS:]]
    for job_id in viejos:
        del jobs[job_id]
        shutil.rmtree(PREVIAS / job_id, ignore_errors=True)
    guardar_jobs()
    # Sin trabajo ni conversión en curso, en el contenedor solo deben existir tini (pid 1) y este proceso.
    ocupado = bool(pdf_activos) or any(j["status"] == "running" for j in jobs.values())
    atorados = 0
    if not ocupado:
        for d in Path("/proc").iterdir():
            if d.name.isdigit() and int(d.name) not in (1, os.getpid()):
                try:
                    os.kill(int(d.name), signal.SIGKILL)
                    atorados += 1
                except OSError:
                    pass
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass
    return {"antes_mb": round(antes, 1), "despues_mb": round(_rss_mb(), 1), "jobs_borrados": len(viejos),
            "atorados": atorados, "ocupado": ocupado}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/jobs")
async def create_job(body: JobIn):
    cwd = body.cwd
    if body.resume:  # una sesión solo se puede continuar desde la carpeta donde se creó
        sesion = await asyncio.to_thread(_buscar_sesion, body.resume)
        if not sesion:
            raise HTTPException(404, "no existe esa sesión")
        cwd = sesion["cwd"]
    resolve_cwd(cwd)
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "status": "queued", "prompt": body.prompt, "cwd": cwd,
                    "model": body.model, "max_turns": body.max_turns, "disallowed_tools": body.disallowed_tools,
                    "resume": body.resume, "timeout_s": body.timeout_s, "escuela": body.escuela, "created": time.time()}
    guardar_jobs()
    await queue.put(job_id)
    return {"id": job_id, "position": queue.qsize(), "cwd": cwd}


_historial: dict[str, tuple[float, dict | None]] = {}  # log -> (mtime, resumen); los logs no cambian ya escritos


def _resumen_log(f: Path) -> dict | None:
    """Saca de un log de trabajo el JSON que imprimió `claude -p` (tokens, costo, modelos)."""
    texto = f.read_text(errors="replace")
    cabecera = texto.split("\n", 1)[0]
    cuerpo = texto.rpartition("\n== STDERR\n")[0].rpartition("\n== STDOUT\n")[2]
    try:
        data = json.loads(cuerpo or "{}")
    except json.JSONDecodeError:
        data = {}
    modelos = {
        m: {"entrada": u.get("inputTokens", 0), "cache_lectura": u.get("cacheReadInputTokens", 0),
            "cache_escritura": u.get("cacheCreationInputTokens", 0), "salida": u.get("outputTokens", 0),
            "costo_usd": u.get("costUSD", 0)}
        for m, u in (data.get("modelUsage") or {}).items()
    }
    return {
        "ts": f.stat().st_mtime,
        "cwd": cabecera.removeprefix("== CMD cwd=").removeprefix(str(WORKSPACE)).strip("/"),
        "estado": "error" if not data or data.get("is_error") else "done",
        "costo_usd": data.get("total_cost_usd") or 0,
        "modelos": modelos,
    }


@app.get("/historial")
async def historial():
    def leer():
        vistos = set()
        for f in LOG_DIR.glob("*.log"):
            vistos.add(f.name)
            mtime = f.stat().st_mtime
            if _historial.get(f.name, (None,))[0] != mtime:
                try:
                    _historial[f.name] = (mtime, _resumen_log(f))
                except OSError:
                    continue
        for nombre in set(_historial) - vistos:
            del _historial[nombre]
        return sorted((r for _, r in _historial.values() if r), key=lambda r: r["ts"])
    return await asyncio.to_thread(leer) if LOG_DIR.is_dir() else []


@app.get("/sesiones")
async def sesiones(n: int = 10):
    """Sesiones pasadas de Claude, la más reciente primero."""
    def leer():
        archivos = sorted(PROYECTOS.glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        out = []
        for f in archivos:
            if s := _leer_sesion(f):
                out.append(s)
            if len(out) >= n:
                break
        return out
    return await asyncio.to_thread(leer)


def _skills_instaladas() -> list[str]:
    if not SKILLS.is_dir():
        return []
    return sorted(d.name for d in SKILLS.iterdir() if (d / "SKILL.md").is_file())


@app.get("/skills")
async def skills_listar():
    return {"skills": await asyncio.to_thread(_skills_instaladas), "plugins": [p.parent.name for p in _plugins()]}


class SkillIn(BaseModel):
    paquete: str  # "owner/repo", una URL, o el comando completo "npx skills add owner/repo -s nombre"


_BANDERAS = {"-s": True, "--skill": True, "--full-depth": False}  # las demás las pone el worker; True = lleva valor


@app.post("/skills")
async def skills_instalar(body: SkillIn):
    """Instala skills GLOBALES (~/.claude/skills, las mismas que usa Claude Code en la Pi). No usa Claude."""
    try:
        tokens = shlex.split(body.paquete)
    except ValueError:
        raise HTTPException(400, "comando mal escrito")
    while tokens and (tokens[0] in ("npx", "-y", "--yes", "add", "a") or tokens[0].startswith("skills")):
        tokens.pop(0)
    args, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-g", "--global", "-y", "--yes"):
            pass
        elif t in _BANDERAS:
            args.append(t)
            if _BANDERAS[t]:
                i += 1
                if i >= len(tokens):
                    raise HTTPException(400, f"falta el valor de {t}")
                args.append(tokens[i])
        elif t.startswith("-") or not re.fullmatch(r"[\w@.:/+~*-]+", t):
            raise HTTPException(400, f"no se permite «{t}»")
        else:
            args.append(t)
        i += 1
    if not args or args[0].startswith("-"):
        raise HTTPException(400, "falta el paquete, p. ej. remotion-dev/skills")
    antes = set(await asyncio.to_thread(_skills_instaladas))
    global pdf_activos
    pdf_activos += 1
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx", "-y", "skills", "add", *args, "-g", "-y", "-a", "claude-code", cwd="/tmp",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=os.environ | {"CI": "1", "NO_COLOR": "1"})
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "la instalación tardó demasiado")
    finally:
        pdf_activos -= 1
    despues = await asyncio.to_thread(_skills_instaladas)
    salida = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.decode(errors="replace"))
    return {"ok": proc.returncode == 0, "nuevas": sorted(set(despues) - antes), "total": len(despues),
            "salida": salida[-1200:]}


class ComprimirIn(BaseModel):
    ruta: str  # ruta de la API ("SEMESTRE_1/…", "Creaciones/…", "JARVIS/…")


@app.post("/comprimir")
async def comprimir(body: ComprimirIn):
    """Copia del video que cabe en Telegram (bots: 50 MB). Va a /data/tmp, nunca junto al original."""
    origen = resolve_cwd(body.ruta)
    if not origen.is_file():
        raise HTTPException(404, "no existe ese archivo")
    info = await asyncio.to_thread(revisar._ffprobe, origen)
    dur = float(info.get("format", {}).get("duration") or 0)
    if dur <= 0:
        raise HTTPException(400, "no es un video")
    kbps = max(300, int(42 * 8 * 1024 / dur) - 128)  # ~42 MB en total, 128 kbps para el audio
    TMP.mkdir(parents=True, exist_ok=True)
    for viejo in TMP.glob("*.mp4"):  # solo vive lo suficiente para mandarse
        if time.time() - viejo.stat().st_mtime > 3600:
            viejo.unlink(missing_ok=True)
    salida = TMP / f"{uuid.uuid4().hex[:8]}_{origen.stem[:40]}.mp4"
    global pdf_activos
    pdf_activos += 1
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(origen), "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 2}k", "-bufsize", f"{kbps * 2}k",
            "-vf", "scale='min(1920,iw)':-2", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(salida),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "comprimir tardó demasiado")
    finally:
        pdf_activos -= 1
    if not salida.is_file():
        raise HTTPException(500, err.decode(errors="replace")[-300:])
    return {"tmp": salida.name, "mb": round(salida.stat().st_size / 1048576, 1)}


@app.get("/archivo/{nombre}")
async def archivo_tmp(nombre: str):
    """Baja una vista previa (/data/previas/<job>/<n>.jpg → 'job_n.jpg') o una copia comprimida de /data/tmp."""
    if re.fullmatch(r"[0-9a-f]{8}_\d{2}\.jpg", nombre):
        p = PREVIAS / nombre[:8] / nombre[9:]
    elif re.fullmatch(r"[0-9a-f]{8}_[^/]+\.mp4", nombre):
        p = TMP / nombre
    else:
        raise HTTPException(400)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/jobs")
async def list_jobs():
    return sorted(
        ({k: v for k, v in j.items() if k != "prompt"} for j in jobs.values()),
        key=lambda j: j["created"], reverse=True,
    )[:20]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    return jobs[job_id]


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] == "queued":
        job["status"] = "cancelled"
    elif job["status"] == "running":
        job["cancel_requested"] = True
        if proc := procs.get(job_id):
            proc.terminate()
    guardar_jobs()
    return {"status": job["status"]}
