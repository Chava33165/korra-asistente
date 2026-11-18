"""Pruebas de empleos.py (vacantes y contactos) contra una carpeta temporal, nunca la real.

Correr (desde Servicios/asistente):
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/bot":/app:ro -w /app asistente-bot python -m unittest discover -s tests -v
(sin los volúmenes de compose: así ninguna prueba puede tocar ~/KORRA ni la base real)
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
empleos = None  # se importa en setUpClass: al cargar el modulo no se toca jarvis (test_memoria pone su carpeta)


def _importar():
    """Importa empleos con jarvis apuntando SIEMPRE a una carpeta temporal (nunca /jarvis = ~/KORRA real)."""
    global empleos
    if "jarvis" not in sys.modules:
        # Forzado (no setdefault): dentro de `docker compose run bot` JARVIS_DIR ya apunta a la carpeta real.
        os.environ["JARVIS_DIR"] = str(TMP / "korra")
        (TMP / "korra" / "diario").mkdir(parents=True, exist_ok=True)
    import empleos as mod
    import jarvis
    assert str(jarvis.BASE).startswith(tempfile.gettempdir()), f"las pruebas apuntan a {jarvis.BASE}: nunca a la carpeta real"
    empleos = mod


V1 = {"puesto": "Becario de Soporte TI", "empresa": "Continental", "perfil": "software", "modalidad": "Presencial",
      "zona": "Tlaquepaque", "prioridad": 1, "ingles": "lectura", "aprende_ingles": True, "sueldo": "$9,000",
      "link": "https://jobs.example.com/123?utm=x", "match": 4, "por_que": "Pide Linux y redes."}
V2 = {"puesto": "Técnico de Pruebas ICT", "empresa": "Jabil", "perfil": "electronica", "prioridad": 1, "ingles": "no",
      "link": "https://jabil.example.com/9", "match": 5}


class Empleos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _importar()

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        empleos.VACANTES_JSON = base / "Vacantes" / "vacantes.json"
        empleos.BUSQUEDA_JSON = base / "Vacantes" / "busqueda.json"
        empleos.CONTACTOS_JSON = base / "Correos" / "contactos.json"
        empleos.PEDIDOS_CV = base / "Vacantes" / "pedidos_cv"

    def test_integrar_sin_perder_estado(self):
        nuevas = empleos.integrar([V1, V2, {"puesto": "sin link", "empresa": "X"}], "2026-11-14", avisado=True)
        self.assertEqual([v["empresa"] for v in nuevas], ["Jabil", "Continental"])  # prioridad 1, mejor match primero
        vid = next(v["id"] for v in nuevas if v["empresa"] == "Continental")
        empleos.poner_estado(vid, "me_interesa")
        # la misma vacante otra vez (link con otro query): no es nueva y conserva su estado
        otra = dict(V1, link="https://jobs.example.com/123?utm=y", sueldo="$10,000")
        self.assertEqual(empleos.integrar([otra], "2026-11-17", avisado=False), [])
        v = next(x for x in empleos.cargar() if x["id"] == vid)
        self.assertEqual((v["estado"], v["sueldo"], v["encontrada"]), ("me_interesa", "$10,000", "2026-11-14"))

    def test_limpia_valores_raros(self):
        [v] = empleos.integrar([dict(V2, perfil="otro", ingles="mucho", match="9", prioridad=None)], "2026-11-14", True)
        self.assertEqual((v["perfil"], v["ingles"], v["match"], v["prioridad"]), ("software", "no", 5, 2))

    def test_activas_y_teclado(self):
        nuevas = empleos.integrar([V1, V2], "2026-11-14", True)
        empleos.poner_estado(nuevas[0]["id"], "descartada")
        self.assertEqual([v["empresa"] for v in empleos.activas()], ["Continental"])
        botones = empleos.teclado(empleos.activas()).inline_keyboard[0]
        self.assertEqual(len(botones), 2)
        self.assertTrue(all(len(b.callback_data) <= 64 for b in botones))
        self.assertIn("Becario de Soporte TI", empleos.texto_lista(empleos.activas(), "x"))

    def test_contactos_solo_publicados_y_sin_repetir(self):
        n = empleos.integrar_contactos([
            {"empresa": "Acme", "correo": "RH@acme.mx", "fuente": "https://acme.mx/empleos"},
            {"empresa": "Acme", "correo": "rh@acme.mx", "fuente": "https://acme.mx/otra"},
            {"empresa": "Sin fuente", "correo": "a@b.mx", "fuente": ""},
            {"empresa": "Mal", "correo": "no-es-correo", "fuente": "https://x.mx"},
        ], "2026-11-14")
        self.assertEqual(n, 1)
        [c] = json.loads(empleos.CONTACTOS_JSON.read_text())
        self.assertEqual((c["correo"], c["estado"]), ("rh@acme.mx", "pendiente"))

    def test_leer_busqueda(self):
        self.assertIsNone(empleos.leer_busqueda())
        empleos.BUSQUEDA_JSON.parent.mkdir(parents=True, exist_ok=True)
        empleos.BUSQUEDA_JSON.write_text(json.dumps({"vacantes": [V1], "contactos": []}))
        self.assertEqual(empleos.leer_busqueda(), ([V1], []))

    def test_prompt_cv(self):
        p = empleos.prompt_cv(dict(V2, id="x"))
        self.assertIn("REGLAS_CV.md", p)
        self.assertIn("Técnico de Pruebas ICT", p)
        self.assertIn("Busco becario de soporte", empleos.prompt_cv_texto("  Busco becario de soporte  "))

    def test_pedidos_cv_de_la_app(self):
        self.assertEqual(empleos.pedidos_cv(), [])
        empleos.PEDIDOS_CV.mkdir(parents=True)
        (empleos.PEDIDOS_CV / "2_v2.json").write_text(json.dumps({"id": "v2"}))
        (empleos.PEDIDOS_CV / "1_v1.json").write_text(json.dumps({"id": "v1"}))
        (empleos.PEDIDOS_CV / "3_roto.json").write_text("no es json")
        self.assertEqual([p["id"] for _, p in empleos.pedidos_cv()], ["v1", "v2"])
        self.assertFalse((empleos.PEDIDOS_CV / "3_roto.json").exists())

    def test_vacante_de_cv(self):
        texto = "Becario de TI en Difarvet S.A. de C.V. Funciones: soporte Linux"
        v = empleos.vacante_de_cv({"puesto": "Becario de TI", "empresa": "Difarvet", "link": "no es url"}, texto)
        self.assertEqual((v["estado"], v["link"], v["perfil"], v["detalles"]), ("me_interesa", "", "software", texto))
        self.assertEqual(empleos.nombre_cv(v), "Salvador_Pantoja_CV_Difarvet_Becario_de_TI")
        self.assertIn(texto, empleos.prompt_cv(v))
        otra = empleos.vacante_de_cv({"puesto": "becario de ti", "empresa": "DIFARVET"}, "otra vez")
        self.assertEqual((otra["id"], len(empleos.cargar())), (v["id"], 1))  # no se duplica
        self.assertEqual(empleos.vacante_de_cv({}, "x")["puesto"], "Puesto sin nombre")

    def test_buscar_vacantes(self):
        empleos.integrar([V1, V2], "2026-11-14", True)
        self.assertEqual([v["empresa"] for v in empleos.buscar_vacantes("la de continental")], ["Continental"])
        self.assertEqual([v["empresa"] for v in empleos.buscar_vacantes("tecnico pruebas")], ["Jabil"])
        self.assertEqual(empleos.buscar_vacantes("Google"), [])
        self.assertEqual(empleos.buscar_vacantes("la"), [])

    def test_me_interesa_cv_y_aplicar(self):
        [v2, v1] = empleos.integrar([V1, V2], "2026-11-14", True)
        v = empleos.pedir_cv(v1["id"])
        self.assertEqual((v["estado"], v["cv_carpeta"]), ("me_interesa", "Software"))
        self.assertEqual(v["cv_archivo"], "Salvador_Pantoja_CV_Continental_Becario_de_Soporte_TI")
        self.assertIn(v["cv_archivo"], empleos.prompt_cv(v))
        self.assertIsNone(empleos.cv_listo(v))
        cvs = Path(tempfile.mkdtemp())
        viejo, empleos.DIR = empleos.DIR, cvs
        try:
            (cvs / "CVs" / "Software").mkdir(parents=True)
            pdf = cvs / "CVs" / "Software" / f"{v['cv_archivo']}.pdf"
            pdf.write_bytes(b"%PDF")
            self.assertEqual(empleos.cv_listo(v), pdf)
        finally:
            empleos.DIR = viejo
        a = empleos.poner_estado(v1["id"], "aplicada")
        self.assertTrue(a["fecha_aplicada"])
        primera = a["fecha_aplicada"]
        self.assertEqual(empleos.poner_estado(v1["id"], "aplicada")["fecha_aplicada"], primera)
        self.assertEqual(empleos.pedir_cv(v1["id"])["estado"], "aplicada")  # pedir otro CV no la "desaplica"

    def test_paginas_de_puestos(self):
        muchas = [dict(V1, puesto=f"Becario {k}", link=f"https://x.mx/{k}") for k in range(19)]
        empleos.integrar(muchas, "2026-11-14", True)
        texto, teclado, total = empleos.pagina_puestos(0)
        self.assertEqual(total, 19)
        self.assertIn("página 1 de 3", texto)
        nav = teclado.inline_keyboard[-1]
        self.assertEqual([b.callback_data for b in nav], ["emp:pag:1"])
        texto, teclado, _ = empleos.pagina_puestos(2)
        self.assertIn("página 3 de 3", texto)
        self.assertIn("17.", texto)
        self.assertEqual([b.callback_data for b in teclado.inline_keyboard[-1]], ["emp:pag:1"])
        self.assertTrue(all(len(b.callback_data) <= 64 for fila in teclado.inline_keyboard for b in fila))

    def test_lista_cvs_maestros_primero(self):
        cvs = Path(tempfile.mkdtemp())
        viejo = empleos.DIR
        try:
            empleos.DIR = cvs
            for rel in ("CVs/00_Maestros/Salvador_Pantoja_CV_Electronica.pdf", "CVs/Software/A.pdf", "CVs/Electronica/B.pdf"):
                (cvs / rel).parent.mkdir(parents=True, exist_ok=True)
                (cvs / rel).write_bytes(b"%PDF")
            self.assertEqual([f.name for f in empleos.lista_cvs()][0], "Salvador_Pantoja_CV_Electronica.pdf")
            self.assertEqual(len(empleos.lista_cvs()), 3)
        finally:
            empleos.DIR = viejo


if __name__ == "__main__":
    unittest.main()
