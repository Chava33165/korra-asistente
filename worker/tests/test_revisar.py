"""Pruebas de la revisión de entregables y de qué archivos cuenta el worker (carpetas temporales).

Correr (desde Servicios/asistente):
  docker compose run --rm --no-deps -v ./worker:/app -w /app worker python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
for d in ("workspace", "jarvis", "creaciones", "data"):
    (TMP / d).mkdir()
os.environ.update(WORKSPACE=str(TMP / "workspace"), JARVIS=str(TMP / "jarvis"), CREACIONES=str(TMP / "creaciones"),
                  LOG_DIR=str(TMP / "data" / "logs"), ESTADO_JOBS=str(TMP / "data" / "jobs.json"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import revisar  # noqa: E402
import worker  # noqa: E402


class RevisarTest(unittest.TestCase):
    def test_docx_escolar_sin_portada_ni_prefijo(self):
        from docx import Document
        d = Document()
        for _ in range(5):
            d.add_paragraph("La globalización económica cambió el comercio entre países de todo el mundo. " * 3)
        p = TMP / "tarea.docx"
        d.save(p)
        r = revisar.revisar_archivo(p, TMP / "previa_docx", escuela=True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("portada" in x for x in r["problemas"]))
        self.assertTrue(any("26310453_" in x for x in r["problemas"]))

    def test_pptx(self):
        from pptx import Presentation
        pr = Presentation()
        for _ in range(3):
            pr.slides.add_slide(pr.slide_layouts[0])
        p = TMP / "26310453_Expo.pptx"
        pr.save(p)
        r = revisar.revisar_archivo(p, TMP / "previa_pptx", escuela=True)
        self.assertTrue(r["ok"], r)
        self.assertIn("3 diapositiva(s)", r["detalles"])

    def test_video_con_previa(self):
        p = TMP / "video.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                        "-pix_fmt", "yuv420p", str(p)], check=True)
        r = revisar.revisar_archivo(p, TMP / "previa_mp4", escuela=False)
        self.assertTrue(r["ok"], r)
        self.assertIn("640×360", r["detalles"])
        self.assertTrue(Path(r["previa"]).is_file())


class WorkerTest(unittest.TestCase):
    def test_snapshot_salta_node_modules_y_usa_rutas_de_api(self):
        base = TMP / "creaciones" / "Videos" / "2026-11-08 Prueba"
        (base / "_proyecto" / "node_modules" / "x").mkdir(parents=True)
        (base / "_proyecto" / "node_modules" / "x" / "index.js").write_text("x")
        (base / "Prueba.mp4").write_bytes(b"0")
        snap = worker.snapshot(base)
        self.assertEqual(list(snap), ["Creaciones/Videos/2026-11-08 Prueba/Prueba.mp4"])

    def test_entregables_del_manifiesto(self):
        base = TMP / "workspace" / "SEMESTRE_1" / "CULTURA" / "P2"
        (base / ".korra").mkdir(parents=True)
        (base / "26310453_Ensayo.docx").write_bytes(b"0")
        (base / "notas.py").write_text("x")
        job = {"started": time.time() - 5, "changed": ["SEMESTRE_1/CULTURA/P2/notas.py"]}
        (base / ".korra" / "entrega.json").write_text(json.dumps(
            {"archivos": [str(base / "26310453_Ensayo.docx"), "no-existe.pdf"], "resumen": "ok"}))
        self.assertEqual(worker.entregables(job, base), ["SEMESTRE_1/CULTURA/P2/26310453_Ensayo.docx"])

    def test_entregables_sin_manifiesto_filtra_codigo(self):
        base = TMP / "workspace" / "OTROS"
        base.mkdir(parents=True, exist_ok=True)
        job = {"started": time.time(), "changed": ["OTROS/app.py", "OTROS/26310453_Mapa.pdf", "_TELEGRAM/entrada/x.pdf"]}
        self.assertEqual(worker.entregables(job, base), ["OTROS/26310453_Mapa.pdf"])

    def test_tipo_error_limite(self):
        self.assertEqual(worker.tipo_error({"status": "error"}, {"result": "You've hit your session limit"}, b""), "limite")
        self.assertEqual(worker.tipo_error({"status": "timeout"}, {}, b""), "timeout")


if __name__ == "__main__":
    unittest.main()
