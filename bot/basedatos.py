"""
Base de datos de KORRA (SQLite, un solo archivo: /data/bot.db) con migraciones versionadas.

Cómo agregar algo en el futuro (una tabla, una columna, un índice…):
  1. Agrega una función al FINAL de MIGRACIONES. Nunca edites ni reordenes las que ya existen.
  2. Al arrancar, el bot aplica solo las que falten. SQLite guarda en qué versión va (PRAGMA user_version).
  3. Antes de migrar se copia la base a /data/respaldos/ (se guardan las últimas 10).

Para atributos que cambian según el tipo de dato, no hace falta columna nueva: usa la columna JSON `extra`
(se consulta con json_extract(extra, '$.campo')). Si un campo se vuelve muy usado, se "asciende" a columna
con una migración.
"""
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("bot")
DB_PATH = Path(os.environ.get("DB_PATH", "/data/bot.db"))
RESPALDOS = DB_PATH.parent / "respaldos"
FTS = "tokenize='unicode61 remove_diacritics 2'"  # búsquedas sin importar acentos ni mayúsculas


def _columnas(db: sqlite3.Connection, tabla: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({tabla})")}


def _agregar_columna(db: sqlite3.Connection, tabla: str, columna: str, tipo: str):
    if columna not in _columnas(db, tabla):
        db.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


# ---------------------------------------------------------------- migraciones (en orden, solo se agregan)

def m001_tablas_base(db):
    """Las tablas que ya existían antes del sistema de migraciones (idempotente)."""
    db.execute("CREATE TABLE IF NOT EXISTS msgs (id INTEGER PRIMARY KEY, chat INTEGER, role TEXT, content TEXT, ts REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS tareas (
        id TEXT PRIMARY KEY, curso_id TEXT, curso TEXT, materia TEXT, titulo TEXT, descripcion TEXT,
        link TEXT, entrega TEXT, carpeta TEXT, estado TEXT, avisos TEXT DEFAULT '[]',
        pospuesta REAL DEFAULT 0, evento_id TEXT, creada REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS anuncios (
        id TEXT PRIMARY KEY, curso TEXT, fecha TEXT, texto TEXT, fechas TEXT DEFAULT '[]', visto REAL)""")


def m002_tareas_en_rojo(db):
    _agregar_columna(db, "tareas", "en_rojo", "INTEGER DEFAULT 0")


def m003_recuerdos(db):
    """Memoria de largo plazo: un dato por fila, con categoría, importancia y caducidad."""
    db.execute("""CREATE TABLE IF NOT EXISTS recuerdos (
        id INTEGER PRIMARY KEY,
        categoria TEXT NOT NULL,             -- perfil, escuela, trabajo, familia, salud, dinero, gustos, ...
        texto TEXT NOT NULL,
        importancia INTEGER DEFAULT 2,       -- 3 = núcleo (siempre en la cabeza), 2 = normal, 1 = detalle
        vigente_hasta TEXT,                  -- AAAA-MM-DD; al pasar se marca 'vencido' (no se borra)
        estado TEXT DEFAULT 'activo',        -- activo | vencido | olvidado
        fuente TEXT,                         -- chat | reflexion | migracion | chava
        extra TEXT DEFAULT '{}',             -- JSON libre (atributos por tipo, historial de cambios…)
        creado REAL, actualizado REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_recuerdos_estado ON recuerdos(estado, categoria, importancia)")
    db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS recuerdos_fts USING fts5(
        texto, categoria, content='recuerdos', content_rowid='id', {FTS})""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS recuerdos_ai AFTER INSERT ON recuerdos BEGIN
        INSERT INTO recuerdos_fts(rowid, texto, categoria) VALUES (new.id, new.texto, new.categoria); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS recuerdos_ad AFTER DELETE ON recuerdos BEGIN
        INSERT INTO recuerdos_fts(recuerdos_fts, rowid, texto, categoria) VALUES ('delete', old.id, old.texto, old.categoria); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS recuerdos_au AFTER UPDATE ON recuerdos BEGIN
        INSERT INTO recuerdos_fts(recuerdos_fts, rowid, texto, categoria) VALUES ('delete', old.id, old.texto, old.categoria);
        INSERT INTO recuerdos_fts(rowid, texto, categoria) VALUES (new.id, new.texto, new.categoria); END""")


def m004_buscador_platicas(db):
    """Índice de texto completo sobre todo el historial de pláticas y sobre el diario."""
    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts USING fts5(content, content='msgs', content_rowid='id', {FTS})")
    db.execute("""CREATE TRIGGER IF NOT EXISTS msgs_ai AFTER INSERT ON msgs BEGIN
        INSERT INTO msgs_fts(rowid, content) VALUES (new.id, new.content); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS msgs_ad AFTER DELETE ON msgs BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, content) VALUES ('delete', old.id, old.content); END""")
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES ('rebuild')")  # indexa lo que ya había
    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS diario_fts USING fts5(fecha UNINDEXED, texto, {FTS})")


def m005_uso(db):
    """Tokens y dinero de cada llamada a un modelo, y qué herramientas usa KORRA (para las estadísticas)."""
    db.execute("""CREATE TABLE IF NOT EXISTS uso_llm (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL,
        proveedor TEXT, modelo TEXT, origen TEXT,  -- origen: chat, proactivo, reflexion, anuncio, redactor
        entrada INTEGER DEFAULT 0,                 -- tokens de entrada sin caché
        entrada_cache INTEGER DEFAULT 0,           -- tokens de entrada que salieron de la caché (mucho más baratos)
        salida INTEGER DEFAULT 0, costo_usd REAL DEFAULT 0)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_uso_llm_ts ON uso_llm(ts)")
    db.execute("CREATE TABLE IF NOT EXISTS uso_herramientas (ts REAL NOT NULL, nombre TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_uso_herr_ts ON uso_herramientas(ts)")


MIGRACIONES = [
    m001_tablas_base,
    m002_tareas_en_rojo,
    m003_recuerdos,
    m004_buscador_platicas,
    m005_uso,
]


# ---------------------------------------------------------------- conexión

def _respaldar(version: int):
    if not DB_PATH.exists():
        return
    RESPALDOS.mkdir(parents=True, exist_ok=True)
    shutil.copy(DB_PATH, RESPALDOS / f"bot_v{version}_{time.strftime('%Y%m%d_%H%M%S')}.db")
    for viejo in sorted(RESPALDOS.glob("bot_v*.db"))[:-10]:
        viejo.unlink()


def conectar() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")  # lecturas y escrituras sin bloquearse entre sí
    version = db.execute("PRAGMA user_version").fetchone()[0]
    pendientes = MIGRACIONES[version:]
    if pendientes:
        _respaldar(version)
        for i, mig in enumerate(pendientes, start=version + 1):
            with db:  # cada migración es atómica: o se aplica completa o nada
                mig(db)
                db.execute(f"PRAGMA user_version = {i}")
            log.info("Migración %d aplicada: %s", i, mig.__name__)
    return db
