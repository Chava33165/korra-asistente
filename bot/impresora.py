"""
Impresora de la casa (HP Smart Tank 210-220) a través del CUPS de la Raspberry Pi.

El contenedor no habla con la impresora directo: usa el socket de CUPS del host (/run/cups, montado en
docker-compose) con los comandos `lp`, `lpstat` y `cancel`. Así CUPS no se abre a la red y los filtros que
convierten PDF/imagen al formato de la impresora corren en el host.
"""
import os
import re
import subprocess
from pathlib import Path

IMPRESORA = os.environ.get("IMPRESORA", "HP_Smart_Tank")
IMPRESORA_IP = os.environ.get("IMPRESORA_IP", "192.168.1.140")  # solo para leer tinta/papel (opcional)

DIRECTOS = {".pdf", ".jpg", ".jpeg", ".png", ".txt"}                      # CUPS los imprime tal cual
CONVERTIBLES = {".docx", ".doc", ".odt", ".rtf", ".pptx", ".ppt", ".xlsx", ".xls", ".md"}  # pasan por LibreOffice
CALIDADES = {"borrador": "Draft", "normal": "Normal", "alta": "High"}
TAMANOS = {"carta": "Letter", "oficio": "Legal", "a4": "A4", "foto 4x6": "4x6", "foto 5x7": "5x7"}


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "La impresora no respondió a tiempo."
    except FileNotFoundError:
        return 1, "Falta el cliente de impresión (cups-client) en el contenedor."


def imprimible(ruta: Path) -> bool:
    return ruta.suffix.lower() in DIRECTOS | CONVERTIBLES


def paginas(ruta: Path) -> int | None:
    if ruta.suffix.lower() != ".pdf":
        return 1 if ruta.suffix.lower() in (".jpg", ".jpeg", ".png") else None
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(ruta)).pages)
    except Exception:
        return None


def estado() -> dict:
    """{'lista': bool, 'texto': 'lista / imprimiendo / …', 'avisos': [...], 'tinta': {'negro': 100, …}}"""
    rc, out = _run(["lpstat", "-p", IMPRESORA])
    if rc != 0:
        return {"lista": False, "texto": "no encuentro la impresora en el servidor", "avisos": [out[:150]], "tinta": {}}
    texto = "lista" if "is idle" in out else "imprimiendo" if "now printing" in out else "detenida" if "disabled" in out else out[:80]
    avisos, tinta = [], {}
    # Tinta y papel: se le pregunta directo a la impresora (si no contesta, no pasa nada)
    rc2, attrs = _run(["ipptool", "-tv", f"ipp://{IMPRESORA_IP}:631/ipp/print", "get-printer-attributes.test"], timeout=12)
    if rc2 == 0:
        nombres = re.search(r"marker-names .*? = (.+)", attrs)
        niveles = re.search(r"marker-levels .*? = (.+)", attrs)
        if nombres and niveles:
            es = {"cyan": "cian", "magenta": "magenta", "yellow": "amarillo", "black": "negro"}
            for n, v in zip(nombres[1].split(","), niveles[1].split(",")):
                tinta[es.get(n.split()[0].lower(), n)] = int(v) if v.strip().lstrip("-").isdigit() else None
        razones = re.search(r"printer-state-reasons .*? = (.+)", attrs)
        r = razones[1] if razones else ""
        for clave, aviso in (("media-empty", "sin papel en la bandeja"), ("media-jam", "papel atorado"),
                             ("door-open", "tapa abierta"), ("cover-open", "tapa abierta"),
                             ("marker-supply-empty", "tinta agotada"), ("marker-supply-low", "tinta baja"),
                             ("offline", "impresora apagada o sin red")):
            if clave in r:
                avisos.append(aviso)
    else:
        avisos.append("no pude preguntarle tinta/papel (¿apagada o cambió de IP?)")
    return {"lista": texto in ("lista", "imprimiendo"), "texto": texto, "avisos": avisos, "tinta": tinta}


def imprimir(pdf_o_imagen: Path, copias: int = 1, color: bool = True, calidad: str = "normal",
             tamano: str = "carta", paginas: str = "") -> str:
    """Manda a imprimir y devuelve el id del trabajo (p. ej. 'HP_Smart_Tank-12')."""
    cmd = ["lp", "-d", IMPRESORA, "-n", str(max(1, min(20, int(copias)))),
           "-o", f"ColorModel={'RGB' if color else 'Gray'}",
           "-o", f"print-color-mode={'color' if color else 'monochrome'}",
           "-o", f"cupsPrintQuality={CALIDADES.get(calidad, 'Normal')}",
           "-o", f"media={TAMANOS.get(tamano, 'Letter')}",
           "-o", "fit-to-page", "-t", pdf_o_imagen.name[:60]]
    if paginas and re.fullmatch(r"[\d,\- ]+", paginas):
        cmd += ["-o", f"page-ranges={paginas.replace(' ', '')}"]
    rc, out = _run(cmd + [str(pdf_o_imagen)], timeout=60)
    m = re.search(r"request id is (\S+)", out)
    if rc != 0 or not m:
        raise RuntimeError(out[:300] or "lp falló")
    return m[1]


def pendiente(trabajo: str) -> bool:
    rc, out = _run(["lpstat", "-W", "not-completed", "-o", IMPRESORA])
    return trabajo in out


def cancelar(trabajo: str) -> str:
    rc, out = _run(["cancel", trabajo])
    return "cancelado" if rc == 0 else out[:150]
