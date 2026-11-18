"""Pruebas del lote de correos de empleo (correos_empleo.py) contra carpetas temporales, nunca las reales.

Correr (desde Servicios/asistente):
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/bot":/app:ro -w /app asistente-bot python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ce = None


def _importar():
    global ce
    if "jarvis" not in sys.modules:
        os.environ["JARVIS_DIR"] = str(TMP / "korra")  # forzado: nunca /jarvis real
        (TMP / "korra" / "diario").mkdir(parents=True, exist_ok=True)
    import correos_empleo as mod
    import jarvis
    assert str(jarvis.BASE).startswith(tempfile.gettempdir()), f"las pruebas apuntan a {jarvis.BASE}"
    ce = mod


def contacto(correo, empresa, **extra):
    return {"correo": correo, "empresa": empresa, "perfil": "software", "fuente": "https://x.mx", "nota": "",
            "encontrado": "2026-11-01", "estado": "pendiente"} | extra


class Lote(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _importar()

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        for nombre in ("CONTACTOS", "ENVIOS", "LOTE", "BORRADORES", "BANDEJA", "CUENTA_JSON"):
            setattr(ce, nombre, base / f"{nombre.lower()}.json")
        ce.PEDIDOS = base / "pedidos"

    def escribir(self, nombre, datos):
        getattr(ce, nombre).write_text(json.dumps(datos))

    def test_calentamiento(self):
        hoy = date(2026, 11, 20)
        self.assertEqual(ce.limite_hoy(hoy, 25, []), 5)                      # cuenta nueva
        semana1 = [{"fecha": "2026-11-17"}] * 3 + [{"fecha": "2026-11-20"}] * 2
        self.assertEqual(ce.limite_hoy(hoy, 25, semana1), 3)                 # 5 - 2 de hoy
        self.assertEqual(ce.limite_hoy(hoy, 25, [{"fecha": "2026-11-10"}]), 10)  # 2a semana
        self.assertEqual(ce.limite_hoy(hoy, 25, [{"fecha": "2026-10-01"}]), 25)
        self.assertEqual(ce.limite_hoy(hoy, 8, [{"fecha": "2026-10-01"}]), 8)

    def test_seleccion_sin_repetir_empresa_y_con_seguimiento(self):
        self.escribir("CONTACTOS", [
            contacto("rh@acme.mx", "Acme"), contacto("talento@acme.mx", "ACME"),           # misma empresa
            contacto("rh@beta.mx", "Beta", estado="no_contactar"),
            contacto("rh@gama.mx", "Gama", estado="enviado", enviado="2026-11-05T09:00:00"),  # toca seguimiento
            contacto("rh@delta.mx", "Delta", estado="enviado", enviado="2026-11-18T09:00:00"),  # aun no
            contacto("rh@eps.mx", "Épsilon"),
        ])
        self.escribir("ENVIOS", [{"correo": "x@eps.mx", "empresa": "Epsilon", "fecha": "2026-10-30T10:00:00"}])  # hace <60 dias
        lote = ce.seleccionar(date(2026, 11, 20), 10)
        self.assertEqual([(x["empresa"], x["tipo"]) for x in lote], [("Gama", "seguimiento"), ("Acme", "primero")])
        self.assertEqual(len(ce.seleccionar(date(2026, 11, 20), 1)), 1)
        self.assertEqual(ce.seleccionar(date(2026, 11, 20), 0), [])

    def test_lote_con_borradores_y_envio(self):
        self.escribir("CONTACTOS", [contacto("rh@acme.mx", "Acme"), contacto("rh@zeta.mx", "Zeta")])
        lote = ce.seleccionar(date(2026, 11, 20), 5)
        self.escribir("BORRADORES", {"correos": [{"correo": "RH@acme.mx", "asunto": "Prácticas", "cuerpo": "Hola"},
                                                  {"correo": "rh@zeta.mx", "asunto": "", "cuerpo": "sin asunto"}]})
        d = ce.armar_lote(lote, date(2026, 11, 20))
        self.assertEqual([x["correo"] for x in d["items"]], ["rh@acme.mx"])
        ce.registrar_envio(d["items"][0], "G1", "2026-11-20T09:31:00")
        c = next(x for x in ce.contactos() if x["correo"] == "rh@acme.mx")
        self.assertEqual((c["estado"], c["enviado"]), ("enviado", "2026-11-20T09:31:00"))
        self.assertEqual(ce.envios()[0]["gmail_id"], "G1")
        self.assertIsNone(ce.quitar(0))  # quitar el unico -> ya no hay lote

    def test_respuestas_rebotes_y_no(self):
        self.escribir("CONTACTOS", [contacto("rh@acme.mx", "Acme", estado="enviado"),
                                    contacto("rh@beta.mx", "Beta", estado="enviado"),
                                    contacto("rh@malo.mx", "Malo", estado="enviado")])
        avisos = ce.procesar_respuestas([
            {"de": "Laura <laura@acme.mx>", "asunto": "Re: Prácticas", "cuerpo": "¿Puedes venir el lunes?"},
            {"de": "rh@beta.mx", "asunto": "Re: CV", "cuerpo": "Gracias, por ahora no estamos contratando."},
            {"de": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "asunto": "Delivery Status",
             "cuerpo": "Your message to rh@malo.mx couldn't be delivered"},
            {"de": "spam@otro.mx", "asunto": "Oferta", "cuerpo": "compra ya"},
        ], "2026-11-21T08:00:00")
        self.assertEqual({(a["empresa"], a["tipo"]) for a in avisos},
                         {("Acme", "respondio"), ("Beta", "no_contactar"), ("Malo", "rebote")})
        self.assertEqual(ce.resumen()["respondio"], 1)

    def test_bandeja_y_pedidos(self):
        self.escribir("CONTACTOS", [contacto("rh@acme.mx", "Acme", estado="enviado")])
        ce.guardar_bandeja([
            {"id": "m1", "hilo": "h1", "de": "Laura <laura@acme.mx>", "asunto": "Re: Prácticas", "fecha": "Mon, 16 Nov 2026 10:00:00 -0600", "resumen": "Hola Salvador"},
            {"id": "m2", "hilo": "h2", "de": "otro@x.mx", "asunto": "Info", "fecha": "Tue, 17 Nov 2026 09:00:00 -0600", "resumen": "..."},
        ])
        b = ce.bandeja()
        self.assertEqual([m["id"] for m in b], ["m2", "m1"])  # lo mas nuevo arriba
        self.assertEqual((b[1]["empresa"], b[1]["correo"]), ("Acme", "laura@acme.mx"))
        self.assertTrue(b[1]["importante"])                 # de una empresa a la que le escribio
        self.assertFalse(b[0]["importante"])                # "Info" de otro@x.mx: no
        ce.guardar_bandeja([{"id": "m3", "hilo": "h3", "de": "empleos@computrabajo.com", "asunto": "Tu próximo empleo está a un clic", "fecha": "", "resumen": ""},
                            {"id": "m4", "hilo": "h4", "de": "info@email.meetup.com", "asunto": "Join 5 Members at Spanish Practice", "fecha": "", "resumen": ""}])
        imp = {m["id"]: m["importante"] for m in ce.bandeja()}
        self.assertEqual((imp["m3"], imp["m4"]), (True, False))
        ce.guardar_bandeja([{"id": "m1", "hilo": "h1", "de": "laura@acme.mx", "asunto": "Re: Prácticas", "fecha": "", "resumen": "x"}])
        self.assertEqual(len(ce.bandeja()), 4)  # no se duplica y conserva la fecha
        self.assertTrue(next(m for m in ce.bandeja() if m["id"] == "m1")["fecha"].startswith("2026-11-16"))
        self.assertEqual(ce.url_gmail("h1"), "https://mail.google.com/mail/u/0/#all/h1")
        ce.guardar_cuenta("salva.empleo@gmail.com")
        self.assertIn("/u/salva.empleo@gmail.com/#all/h1", ce.url_gmail("h1"))
        self.assertEqual(ce.pedidos_pendientes(), [])
        ce.PEDIDOS.mkdir()
        (ce.PEDIDOS / "1_m1.json").write_text(json.dumps({"id": "m1"}))
        (ce.PEDIDOS / "2_roto.json").write_text("no es json")
        self.assertEqual([p["id"] for _, p in ce.pedidos_pendientes()], ["m1"])
        self.assertFalse((ce.PEDIDOS / "2_roto.json").exists())

    def test_adjunto_cae_al_master(self):
        self.assertEqual(ce.adjunto("software").name, "Salvador_Pantoja_CV_Master.pdf")


if __name__ == "__main__":
    unittest.main()
