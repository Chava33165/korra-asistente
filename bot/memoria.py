"""
Memoria de KORRA en capas (SQLite):

  0. Perfil       → perfil.md (jarvis.py): quién es Chava en corto, con tope de tamaño. Va en CADA mensaje.
  1. Núcleo       → recuerdos con importancia 3 + pendientes vigentes. Va en CADA mensaje, repartido entre
                    categorías para que ninguna se quede fuera.
  2. Recuerdos    → un dato por fila (categoría, importancia, caducidad, JSON extra). Los que vienen al caso con
                    lo que Chava acaba de escribir entran solos (`relevantes`); el resto, con `recuerdos_buscar`.
  3. Pláticas     → todo el historial y el diario, con buscador de texto (herramienta `platicas_buscar`).

Nada se borra de verdad: "olvidar" y "vencer" solo cambian el estado; cada cambio de texto guarda el
anterior en extra.historial. `memoria.md` (en JARVIS) es solo una VISTA para Chava: se regenera sola y, si
él la edita, los cambios se regresan a la base (`sincronizar_md`).
"""
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import jarvis

CATEGORIAS = {
    "perfil": "Quién es",
    "preferencias": "Cómo le gusta que le hable / trabaje",
    "escuela": "Escuela",
    "trabajo": "Trabajo",
    "dinero": "Dinero y nóminas",
    "familia": "Familia y personas",
    "salud": "Salud y rutina",
    "gustos": "Gustos y cosas personales",
    "proyectos": "Proyectos y metas",
    "pendiente": "Pendientes",
    "seguimiento": "Seguimientos (preguntarle después)",
    "sistema": "Sistema, rutas y herramientas",
    "otro": "Otros",
}
IMPORTANCIAS = {3: "núcleo", 2: "normal", 1: "detalle"}
MAX_NUCLEO = 5000  # caracteres del núcleo que va en cada mensaje
# Orden en que se reparte el núcleo (por turnos, una línea por categoría a la vez). "perfil" no está: lo cubre perfil.md.
ORDEN_NUCLEO = ["preferencias", "pendiente", "seguimiento", "escuela", "trabajo", "dinero", "familia", "salud",
                "proyectos", "gustos", "sistema", "otro"]
SIMILAR = 0.6  # proporción de palabras en común para considerar que dos recuerdos dicen lo mismo
# Palabras que no sirven para encontrar recuerdos parecidos (ya sin acentos, de más de 3 letras).
VACIAS = {"chava", "korra", "para", "pero", "como", "esta", "este", "esto", "estos", "estas", "tiene", "tengo",
          "hace", "hacer", "porque", "cuando", "donde", "sobre", "entre", "desde", "hasta", "tambien", "solo",
          "todo", "todos", "todas", "nada", "algo", "bien", "muy", "mas", "ella", "ellos", "quiero", "puedes",
          "oye", "sabes", "dime", "pues", "nomas", "ahorita", "entonces", "cosa", "cosas", "verdad", "neta"}

db: sqlite3.Connection | None = None
_ultimo_export = {"hash": ""}


def init(conexion: sqlite3.Connection):
    global db
    db = conexion
    if not db.execute("SELECT 1 FROM recuerdos LIMIT 1").fetchone() and jarvis.MEMORIA.exists():
        n = importar_md(jarvis.MEMORIA.read_text(encoding="utf-8"), fuente="migracion")
        if n:
            import shutil
            shutil.copy(jarvis.MEMORIA, jarvis.MEMORIA.with_name("memoria_antes_de_sqlite.md"))
    indexar_diarios()
    caducar()
    exportar_md()


def _hoy() -> str:
    return jarvis.ahora().strftime("%Y-%m-%d")


def _cat(categoria: str) -> str:
    c = (categoria or "otro").strip().lower()
    for k, nombre in CATEGORIAS.items():
        if c in (k, nombre.lower()):
            return k
    return c if c.isidentifier() else "otro"  # categoría nueva: se permite (escalable), si es un nombre simple


def _fila(r) -> dict:
    return {"id": r[0], "categoria": r[1], "texto": r[2], "importancia": r[3], "vigente_hasta": r[4],
            "estado": r[5], "extra": json.loads(r[6] or "{}")}


COLS = "id, categoria, texto, importancia, vigente_hasta, estado, extra"


def _palabras(t: str) -> set[str]:
    return {w for w in re.findall(r"\w+", jarvis_norm(t)) if len(w) > 3 and w not in VACIAS}


def _parecido(a: set[str], b: set[str]) -> bool:
    """Dos recuerdos dicen lo mismo: muchas palabras en común, o uno contiene al otro ("le gustan (mucho) los tacos…")."""
    if not a or not b:
        return False
    comun = len(a & b)
    return comun / len(a | b) >= SIMILAR or (comun == min(len(a), len(b)) and comun >= 3)


def jarvis_norm(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- escribir

def recordar(texto: str, categoria: str = "otro", importancia: int = 2, vigente_hasta: str | None = None,
             extra: dict | None = None, id: int | None = None, fuente: str = "chat") -> str:
    """Guarda un dato. Con `id` lo actualiza. Si ya existe uno casi igual en la misma categoría, lo actualiza
    en vez de duplicarlo."""
    texto = (texto or "").strip()
    if not texto:
        return "No hay nada que guardar."
    cat, imp, ahora = _cat(categoria), max(1, min(3, int(importancia or 2))), time.time()
    vig = (vigente_hasta or "")[:10] or None
    if id is None:  # ¿ya lo sé? Se busca en su categoría y en los más parecidos de toda la memoria (patrón mem0)
        nuevas = _palabras(texto)
        candidatos = db.execute(f"SELECT {COLS} FROM recuerdos WHERE estado='activo' AND categoria=?", (cat,)).fetchall()
        candidatos += [(r["id"], r["categoria"], r["texto"]) for r in buscar(" ".join(nuevas), n=8)]
        for r in candidatos:
            if _parecido(nuevas, _palabras(r[2])):
                id = r[0]
                break
    if id is not None:
        row = db.execute(f"SELECT {COLS} FROM recuerdos WHERE id=?", (id,)).fetchone()
        if not row:
            return f"No existe el recuerdo #{id}."
        viejo = _fila(row)
        ex = viejo["extra"] | (extra or {})
        if viejo["texto"] != texto:
            ex.setdefault("historial", []).append({"texto": viejo["texto"], "hasta": _hoy()})
            ex["historial"] = ex["historial"][-10:]
        db.execute("UPDATE recuerdos SET texto=?, categoria=?, importancia=?, vigente_hasta=?, estado='activo', "
                   "extra=?, actualizado=? WHERE id=?",
                   (texto, cat, imp, vig, json.dumps(ex, ensure_ascii=False), ahora, id))
        db.commit()
        exportar_md()
        return f"Recuerdo #{id} actualizado (antes decía: «{viejo['texto'][:120]}»)." if viejo["texto"] != texto \
            else f"Ya lo sabía (#{id})."
    cur = db.execute("INSERT INTO recuerdos(categoria, texto, importancia, vigente_hasta, estado, fuente, extra, creado, actualizado) "
                     "VALUES (?,?,?,?, 'activo', ?,?,?,?)",
                     (cat, texto, imp, vig, fuente, json.dumps(extra or {}, ensure_ascii=False), ahora, ahora))
    db.commit()
    exportar_md()
    return f"Guardado como #{cur.lastrowid} ({cat})."


def olvidar(id: int, motivo: str = "") -> str:
    """No borra: lo marca 'olvidado' (se puede recuperar)."""
    row = db.execute("SELECT extra FROM recuerdos WHERE id=?", (id,)).fetchone()
    if not row:
        return f"No existe el recuerdo #{id}."
    ex = json.loads(row[0] or "{}") | {"olvidado": {"fecha": _hoy(), "motivo": motivo}}
    db.execute("UPDATE recuerdos SET estado='olvidado', extra=?, actualizado=? WHERE id=?",
               (json.dumps(ex, ensure_ascii=False), time.time(), id))
    db.commit()
    exportar_md()
    return f"Recuerdo #{id} olvidado."


def caducar() -> int:
    n = db.execute("UPDATE recuerdos SET estado='vencido', actualizado=? WHERE estado='activo' "
                   "AND vigente_hasta IS NOT NULL AND vigente_hasta < ?", (time.time(), _hoy())).rowcount
    db.commit()
    if n:
        exportar_md()
    return n


# ---------------------------------------------------------------- leer / buscar

def _consulta_fts(q: str) -> str:
    """Texto libre → consulta FTS5 segura: cada palabra como prefijo, unidas con OR."""
    palabras = [w for w in re.findall(r"\w+", q) if len(w) > 1][:12]
    return " OR ".join(f'"{w}"*' for w in palabras)


def buscar(q: str = "", categoria: str = "", incluir_viejos: bool = False, n: int = 15) -> list[dict]:
    estados = "('activo','vencido')" if incluir_viejos else "('activo')"
    filtros, params = [f"r.estado IN {estados}"], []
    if categoria:
        filtros.append("r.categoria=?")
        params.append(_cat(categoria))
    if q.strip() and (fts := _consulta_fts(q)):
        sql = (f"SELECT r.id, r.categoria, r.texto, r.importancia, r.vigente_hasta, r.estado, r.extra "
               f"FROM recuerdos_fts f JOIN recuerdos r ON r.id=f.rowid WHERE recuerdos_fts MATCH ? AND "
               + " AND ".join(filtros) + " ORDER BY bm25(recuerdos_fts) LIMIT ?")
        rows = db.execute(sql, [fts, *params, n]).fetchall()
    else:
        rows = db.execute(f"SELECT {COLS} FROM recuerdos r WHERE " + " AND ".join(filtros)
                          + " ORDER BY importancia DESC, actualizado DESC LIMIT ?", [*params, n]).fetchall()
    return [_fila(r) for r in rows]


def buscar_platicas(q: str, n: int = 8) -> list[dict]:
    """Busca en TODO el historial de pláticas y en el diario (no solo los últimos 40 mensajes)."""
    fts = _consulta_fts(q)
    if not fts:
        return []
    res = []
    for mid, rol, ts, frag in db.execute(
            "SELECT m.id, m.role, m.ts, snippet(msgs_fts, 0, '«', '»', '…', 24) FROM msgs_fts JOIN msgs m ON m.id=msgs_fts.rowid "
            "WHERE msgs_fts MATCH ? ORDER BY bm25(msgs_fts) LIMIT ?", (fts, n)):
        res.append({"fecha": datetime.fromtimestamp(ts, jarvis.TZ).strftime("%Y-%m-%d %H:%M"),
                    "quien": "Chava" if rol == "user" else "KORRA", "fragmento": frag})
    for fecha, frag in db.execute(
            "SELECT fecha, snippet(diario_fts, 1, '«', '»', '…', 24) FROM diario_fts WHERE diario_fts MATCH ? "
            "ORDER BY bm25(diario_fts) LIMIT 4", (fts,)):
        res.append({"fecha": fecha, "quien": "diario de KORRA", "fragmento": frag})
    return sorted(res, key=lambda r: r["fecha"])


def indexar_diario(fecha: str, texto: str):
    db.execute("DELETE FROM diario_fts WHERE fecha=?", (fecha,))
    db.execute("INSERT INTO diario_fts(fecha, texto) VALUES (?,?)", (fecha, texto))
    db.commit()


def indexar_diarios():
    for p in sorted(jarvis.DIARIO.glob("*.md")):
        if not db.execute("SELECT 1 FROM diario_fts WHERE fecha=?", (p.stem,)).fetchone():
            indexar_diario(p.stem, p.read_text(encoding="utf-8"))


def conteo() -> dict[str, int]:
    return dict(db.execute("SELECT categoria, count(*) FROM recuerdos WHERE estado='activo' GROUP BY categoria ORDER BY 2 DESC"))


def _linea(r: dict) -> str:
    return f"- [#{r['id']}] {r['texto']}" + (f" (hasta {r['vigente_hasta']})" if r["vigente_hasta"] else "")


def _seleccion_nucleo(con_perfil: bool) -> dict[str, list[dict]]:
    """Importancia 3 + pendientes vigentes, repartidos POR TURNOS entre categorías hasta MAX_NUCLEO: antes se
    llenaba en orden alfabético y categorías enteras (preferencias, seguimientos…) nunca llegaban al prompt."""
    filas = db.execute(f"SELECT {COLS} FROM recuerdos WHERE estado='activo' AND (importancia=3 OR "
                       f"(categoria IN ('pendiente', 'seguimiento') AND (vigente_hasta IS NULL OR vigente_hasta >= ?))) "
                       f"ORDER BY importancia DESC, actualizado DESC", (_hoy(),)).fetchall()
    colas: dict[str, list[dict]] = {}
    for r in map(_fila, filas):
        if r["categoria"] != "perfil" or not con_perfil:
            colas.setdefault(r["categoria"], []).append(r)
    orden = (["perfil"] if not con_perfil else []) + ORDEN_NUCLEO
    orden += [c for c in colas if c not in orden]
    elegidos: dict[str, list[dict]] = {}
    total = 0
    while any(colas.get(c) for c in orden):
        for c in orden:
            if colas.get(c):
                r = colas[c].pop(0)
                if total + len(_linea(r)) <= MAX_NUCLEO:  # la que no cabe se salta; puede caber otra más corta
                    elegidos.setdefault(c, []).append(r)
                    total += len(_linea(r))
    return {c: elegidos[c] for c in orden if c in elegidos}


def nucleo(con_perfil: bool = False) -> str:
    """Lo que KORRA tiene siempre en la cabeza. con_perfil=True: perfil.md ya dice quién es, no se repite."""
    return "\n".join(f"## {CATEGORIAS.get(c, c)}\n" + "\n".join(map(_linea, rs))
                     for c, rs in _seleccion_nucleo(con_perfil).items())


def relevantes(consulta: str, con_perfil: bool = False, n: int = 5) -> str:
    """Recuerdos que vienen al caso con lo que Chava acaba de escribir y que no están ya en el núcleo.
    Así KORRA los tiene sin depender de acordarse de llamar `recuerdos_buscar`."""
    palabras = _palabras(consulta)
    if not palabras:
        return ""
    ya = {r["id"] for rs in _seleccion_nucleo(con_perfil).values() for r in rs}
    filas = [r for r in buscar(" ".join(palabras), n=n + len(ya)) if r["id"] not in ya][:n]
    return "\n".join(f"{_linea(r)} ({CATEGORIAS.get(r['categoria'], r['categoria'])})" for r in filas)


# ---------------------------------------------------------------- vista memoria.md (para Chava)

CABECERA = ("# Lo que sé de Chava\n\n"
            "> Vista generada desde la base de datos de KORRA. Puedes editarla: cambia el texto de una línea,\n"
            "> borra una línea para que la olvide o agrega `- algo nuevo` bajo una sección.\n"
            "> No cambies los `[#número]`. Importancia: ★★★ núcleo · ★★ normal · ★ detalle.\n")


def exportar_md():
    partes = [CABECERA]
    for cat, nombre in list(CATEGORIAS.items()) + [(c, c) for c in conteo() if c not in CATEGORIAS]:
        filas = [_fila(r) for r in db.execute(
            f"SELECT {COLS} FROM recuerdos WHERE estado='activo' AND categoria=? ORDER BY importancia DESC, id", (cat,))]
        if not filas:
            continue
        partes.append(f"\n## {nombre}\n")
        for r in filas:
            partes.append(f"- [#{r['id']}] {'★' * r['importancia']} {r['texto']}"
                          + (f" (hasta {r['vigente_hasta']})" if r["vigente_hasta"] else "") + "\n")
    texto = "".join(partes)
    jarvis.MEMORIA.write_text(texto, encoding="utf-8")
    _ultimo_export["hash"] = hashlib.sha1(texto.encode()).hexdigest()


def sincronizar_md() -> str:
    """Si Chava editó memoria.md, aplica sus cambios a la base."""
    if not jarvis.MEMORIA.exists():
        return ""
    texto = jarvis.MEMORIA.read_text(encoding="utf-8")
    if hashlib.sha1(texto.encode()).hexdigest() == _ultimo_export["hash"]:
        return ""
    vistos, cambios = set(), 0
    antes = {rid for (rid,) in db.execute("SELECT id FROM recuerdos WHERE estado='activo'")}  # solo estos se pueden "borrar"
    cat_actual = "otro"
    nombres = {v.lower(): k for k, v in CATEGORIAS.items()}
    for linea in texto.splitlines():
        if linea.startswith("## "):
            cat_actual = nombres.get(linea[3:].strip().lower(), _cat(linea[3:]))
            continue
        m = re.match(r"^\s*-\s*(?:\[#(\d+)\])?\s*(★*)\s*(.+?)\s*(?:\(hasta (\d{4}-\d{2}-\d{2})\))?\s*$", linea)
        if not m or not m[3]:
            continue
        rid, estrellas, contenido, hasta = m[1], m[2], m[3], m[4]
        imp = len(estrellas) or 2
        if rid:
            rid = int(rid)
            vistos.add(rid)
            row = db.execute(f"SELECT {COLS} FROM recuerdos WHERE id=?", (rid,)).fetchone()
            if row and (row[2] != contenido or row[1] != cat_actual or row[3] != imp or (row[4] or None) != hasta):
                recordar(contenido, cat_actual, imp, hasta, id=rid, fuente="chava")
                cambios += 1
        else:
            recordar(contenido, cat_actual, imp, hasta, fuente="chava")
            cambios += 1
    for rid in antes:
        if rid not in vistos and f"[#{rid}]" not in texto:
            olvidar(rid, "Chava lo borró de memoria.md")
            cambios += 1
    exportar_md()
    return f"{cambios} cambio(s) de memoria.md aplicados"


# ---------------------------------------------------------------- migración desde el memoria.md viejo

SECCION_A_CATEGORIA = {
    "quien es": ("perfil", 3), "escuela": ("escuela", 2), "trabajos y entregables": ("escuela", 2),
    "nominas": ("dinero", 1), "gustos": ("gustos", 2), "integraciones": ("sistema", 1),
    "como le gusta": ("preferencias", 3), "rutas": ("sistema", 1), "pendientes": ("pendiente", 2),
    "familia": ("familia", 2), "trabajo": ("trabajo", 2), "salud": ("salud", 2),
}


def importar_md(texto: str, fuente: str = "migracion") -> int:
    """Importa un memoria.md con secciones '## ' y líneas '- dato' (formato viejo). Devuelve cuántos importó."""
    cat, imp, n = "otro", 2, 0
    for linea in texto.splitlines():
        if linea.startswith("## "):
            seccion = jarvis_norm(linea[3:])
            cat, imp = next(((c, i) for clave, (c, i) in SECCION_A_CATEGORIA.items() if clave in seccion), ("otro", 2))
            continue
        m = re.match(r"^\s*-\s+(.+)$", linea)
        if m and len(m[1].strip()) > 3:
            db.execute("INSERT INTO recuerdos(categoria, texto, importancia, estado, fuente, extra, creado, actualizado) "
                       "VALUES (?,?,?, 'activo', ?, '{}', ?, ?)", (cat, m[1].strip(), imp, fuente, time.time(), time.time()))
            n += 1
    db.commit()
    return n


# ---------------------------------------------------------------- herramientas para DeepSeek

_CATS = ", ".join(CATEGORIAS)
TOOLS = [
    {"type": "function", "function": {
        "name": "recordar",
        "description": ("Guarda en tu memoria algo que aprendiste de Chava (gustos, planes, personas, fechas, cómo se siente, "
                        "cómo le gusta algo). Úsala SOLA, sin pedir permiso. Si ya sabías algo parecido, se actualiza solo "
                        "(no dupliques: si lo ves en tu núcleo o en 'recuerdos que vienen al caso', pasa su id). "
                        "Para CORREGIR un dato que ya tienes, pasa su id."),
        "parameters": {"type": "object", "properties": {
            "texto": {"type": "string", "description": "El dato, completo y claro por sí solo (con fechas absolutas)."},
            "categoria": {"type": "string", "description": f"Una de: {_CATS}."},
            "importancia": {"type": "integer", "enum": [1, 2, 3],
                            "description": ("3 = núcleo: quién es, cómo le gusta que le hables, lo más importante de su vida "
                                            "AHORA (pocos). 2 = útil a futuro. 1 = de un día o anécdota (fue a una fiesta, "
                                            "entregó X): ponle también vigente_hasta.")},
            "vigente_hasta": {"type": "string", "description": "AAAA-MM-DD si deja de aplicar en una fecha (examen, pendiente, viaje). Vacío si es permanente."},
            "id": {"type": "integer", "description": "Solo para corregir/actualizar un recuerdo existente (#id)."}},
            "required": ["texto", "categoria"]}}},
    {"type": "function", "function": {
        "name": "recuerdos_buscar",
        "description": ("Busca en tu memoria de largo plazo (lo que NO está en tu núcleo). Úsala ANTES de decir que no sabes "
                        "algo de Chava, o para ver todo lo de una categoría."),
        "parameters": {"type": "object", "properties": {
            "busqueda": {"type": "string", "description": "Palabras clave (sin importar acentos). Vacío = lista la categoría."},
            "categoria": {"type": "string", "description": f"Opcional: {_CATS}."},
            "incluir_viejos": {"type": "boolean", "description": "true = incluir también datos vencidos."}}}}},
    {"type": "function", "function": {
        "name": "olvidar",
        "description": "Marca un recuerdo como ya no válido (no se borra de verdad). Úsala si algo cambió o era un error.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "motivo": {"type": "string"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "platicas_buscar",
        "description": ("Busca en TODO el historial de pláticas con Chava y en tu diario (tú solo ves los últimos mensajes). "
                        "Para '¿qué te dije de…?', '¿cuándo hablamos de…?', o recordar algo de hace días/semanas."),
        "parameters": {"type": "object", "properties": {"busqueda": {"type": "string"}}, "required": ["busqueda"]}}},
]


def ejecutar(nombre: str, args: dict) -> str | None:
    if nombre == "recordar":
        return recordar(args["texto"], args.get("categoria", "otro"), args.get("importancia", 2),
                        args.get("vigente_hasta"), id=args.get("id"))
    if nombre == "recuerdos_buscar":
        r = buscar(args.get("busqueda", ""), args.get("categoria", ""), bool(args.get("incluir_viejos")))
        return json.dumps([{k: v for k, v in x.items() if k != "extra"} for x in r], ensure_ascii=False) if r else "No encontré nada en tu memoria con eso."
    if nombre == "olvidar":
        return olvidar(int(args["id"]), args.get("motivo", ""))
    if nombre == "platicas_buscar":
        r = buscar_platicas(args["busqueda"])
        return json.dumps(r, ensure_ascii=False) if r else "No encontré nada en las pláticas con eso."
    return None
