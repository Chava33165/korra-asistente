"""Pruebas de la memoria de KORRA, contra una base y una carpeta temporales (nunca la base real).

Correr (desde Servicios/asistente):
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/bot":/app:ro -w /app asistente-bot python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "bot.db")
os.environ["JARVIS_DIR"] = str(TMP / "korra")
(TMP / "korra" / "diario").mkdir(parents=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import basedatos  # noqa: E402
import memoria  # noqa: E402


class MemoriaTest(unittest.TestCase):
    def setUp(self):
        db = basedatos.conectar()
        db.execute("DELETE FROM recuerdos")
        db.commit()
        (TMP / "korra" / "memoria.md").unlink(missing_ok=True)  # si existe, init() lo importaría
        memoria.init(db)

    def test_nucleo_reparte_entre_categorias(self):
        # Mucho de "dinero" (que antes llenaba el núcleo por orden alfabético) no debe dejar fuera "preferencias".
        for i in range(60):  # textos distintos entre sí, para que no se fusionen como duplicados
            memoria.recordar(f"Recibo semana{i:02d} " + " ".join(f"dato{i}x{k}" for k in range(8)), "dinero", 3)
        memoria.recordar("Le gusta que le hablen directo y de compa, sin rodeos", "preferencias", 3)
        memoria.recordar("Preguntarle cómo le fue en el examen de Álgebra", "seguimiento", 2, "2099-01-01")
        texto = memoria.nucleo()
        self.assertIn("directo y de compa", texto)
        self.assertIn("examen de Álgebra", texto)
        self.assertLessEqual(len(texto), memoria.MAX_NUCLEO + 500)  # + encabezados de categoría

    def test_perfil_no_se_repite_en_nucleo(self):
        memoria.recordar("Se llama Salvador y le dicen Chava", "perfil", 3)
        self.assertIn("Salvador", memoria.nucleo())
        self.assertNotIn("Salvador", memoria.nucleo(con_perfil=True))

    def test_duplicado_actualiza_aunque_cambie_la_categoria(self):
        r1 = memoria.recordar("La presentación de Mercosur de Cultura Comparada ya fue expuesta", "escuela", 2)
        r2 = memoria.recordar("Ya expuso la presentación de Mercosur en Cultura Comparada", "otro", 2)
        self.assertIn("Guardado como #", r1)
        self.assertIn("actualizado", r2)
        self.assertEqual(sum(memoria.conteo().values()), 1)

    def test_distintos_no_se_fusionan(self):
        memoria.recordar("Le gustan las películas de cocina", "gustos", 2)
        memoria.recordar("Juega Terraria con sus amigos", "gustos", 2)
        self.assertEqual(memoria.conteo()["gustos"], 2)

    def test_relevantes_trae_lo_que_viene_al_caso_y_no_repite_nucleo(self):
        memoria.recordar("Tiene un servidor de Terraria en la Raspberry", "gustos", 2)
        memoria.recordar("Su materia más difícil es Ecuaciones Diferenciales", "escuela", 3)
        memoria.recordar("Le laten las series de cocina", "gustos", 2)
        r = memoria.relevantes("oye ¿jugamos Terraria hoy?")
        self.assertIn("Terraria", r)
        self.assertNotIn("cocina", r)
        self.assertEqual(memoria.relevantes("¿cómo voy en Ecuaciones Diferenciales?"), "")  # ya está en el núcleo
        self.assertEqual(memoria.relevantes("oye pues"), "")  # solo palabras vacías


if __name__ == "__main__":
    unittest.main()
