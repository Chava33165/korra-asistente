"""Revisión automática de lo que entregó un trabajo de Claude (sin gastar su límite).

Abre cada archivo y dice si está bien hecho en lo básico (abre, tiene páginas/diapositivas, el video dura y tiene
la resolución que dice) y saca una vista previa para mandar por Telegram: un fotograma del video, la primera página
del PDF o la imagen misma. No usa LibreOffice (pesa mucho en RAM para una revisión).
"""
import json
import subprocess
from pathlib import Path

REGISTRO = "26310453"  # registro escolar de Salvador: prefijo de los entregables de la escuela


def _ffprobe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(out or "{}")


def _video(p: Path, previa: Path) -> tuple[list[str], list[str]]:
    info = _ffprobe(p)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return [], ["no tiene pista de video"]
    dur = float(info.get("format", {}).get("duration") or 0)
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = round(int(num) / int(den or 1)) if num.isdigit() and int(den or 1) else 0
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    detalles = [f"{dur:.0f} s", f"{v.get('width')}×{v.get('height')}", f"{fps} fps", "con audio" if audio else "sin audio"]
    problemas = ["dura menos de 1 s"] if dur < 1 else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{min(dur / 3, 3):.2f}", "-i", str(p), "-frames:v", "1",
                    "-vf", "scale='min(720,iw)':-2", str(previa.with_suffix(".jpg"))], timeout=60)
    return detalles, problemas


def _pdf(p: Path, previa: Path) -> tuple[list[str], list[str]]:
    from pypdf import PdfReader
    n = len(PdfReader(str(p)).pages)
    subprocess.run(["pdftoppm", "-jpeg", "-r", "60", "-f", "1", "-l", "1", "-singlefile", str(p),
                    str(previa)], timeout=60)
    return [f"{n} página(s)"], ([] if n else ["no tiene páginas"])


def _docx(p: Path, escuela: bool) -> tuple[list[str], list[str]]:
    from docx import Document
    doc = Document(str(p))
    palabras = sum(len(par.text.split()) for par in doc.paragraphs)
    detalles = [f"~{palabras} palabras", f"~{max(1, round(palabras / 350))} cuartilla(s)"]
    problemas = []
    if escuela:  # la portada institucional lleva el logo y el nombre del CETI en las primeras líneas
        inicio = " ".join(par.text for par in doc.paragraphs[:25]).upper()
        if "CETI" not in inicio and "CENTRO DE ENSEÑANZA" not in inicio and not doc.inline_shapes:
            problemas.append("no le veo portada")
    if palabras < 30:
        problemas.append("casi no tiene texto")
    return detalles, problemas


def _pptx(p: Path) -> tuple[list[str], list[str]]:
    from pptx import Presentation
    n = len(Presentation(str(p)).slides)
    return [f"{n} diapositiva(s)"], ([] if n else ["no tiene diapositivas"])


def _xlsx(p: Path) -> tuple[list[str], list[str]]:
    from openpyxl import load_workbook
    wb = load_workbook(str(p), read_only=True)
    return [f"{len(wb.sheetnames)} hoja(s)"], []


def _imagen(p: Path, previa: Path) -> tuple[list[str], list[str]]:
    info = _ffprobe(p)
    v = next((s for s in info.get("streams", []) if s.get("width")), {})
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-vf", "scale='min(720,iw)':-2",
                    str(previa.with_suffix(".jpg"))], timeout=60)
    return [f"{v.get('width')}×{v.get('height')}"], []


def revisar_archivo(p: Path, previa: Path, escuela: bool) -> dict:
    """`previa`: ruta sin extensión donde dejar la vista previa (se le pone .jpg)."""
    ext = p.suffix.lower()
    res = {"ok": True, "detalles": [], "problemas": [], "previa": ""}
    try:
        if ext in (".mp4", ".mov", ".m4v", ".webm"):
            res["detalles"], res["problemas"] = _video(p, previa)
        elif ext == ".pdf":
            res["detalles"], res["problemas"] = _pdf(p, previa)
        elif ext == ".docx":
            res["detalles"], res["problemas"] = _docx(p, escuela)
        elif ext == ".pptx":
            res["detalles"], res["problemas"] = _pptx(p)
        elif ext == ".xlsx":
            res["detalles"], res["problemas"] = _xlsx(p)
        elif ext in (".png", ".jpg", ".jpeg", ".webp"):
            res["detalles"], res["problemas"] = _imagen(p, previa)
    except Exception as e:  # un archivo que no abre es justo lo que hay que avisar
        res["problemas"].append(f"no se pudo abrir ({type(e).__name__})")
    if p.stat().st_size == 0:
        res["problemas"].append("está vacío")
    if escuela and ext in (".docx", ".pdf", ".pptx", ".xlsx") and not p.name.startswith(REGISTRO + "_"):
        res["problemas"].append(f"el nombre no empieza con {REGISTRO}_")
    if previa.with_suffix(".jpg").is_file():
        res["previa"] = str(previa.with_suffix(".jpg"))
    res["ok"] = not res["problemas"]
    return res
