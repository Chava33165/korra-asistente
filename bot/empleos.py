"""
Busqueda de empleo para Chava (mismo patron que los eventos de software en GDL, ver bot.py).

Claude (worker) investiga vacantes con los dos perfiles de Chava (software y electronica) y deja el resultado en
Empleo/Vacantes/busqueda.json; aqui se integra a vacantes.json (con su estado), que tambien lee y cambia la
pagina Empleo (~/Servicios/empleo). Los contactos publicados para mandar CV van a Empleo/Correos/contactos.json
(los usa el lote diario de correos).

Carpeta: ~/Documentos/Personal/Empleo = /jarvis/archivos/Empleo (bot y worker) = "JARVIS/archivos/Empleo" (API).
"""
import html
import json
import os
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import jarvis

DIR = jarvis.BASE / "archivos" / "Empleo"
CWD = "JARVIS/archivos/Empleo"
VACANTES_JSON = DIR / "Vacantes" / "vacantes.json"
PEDIDOS_CV = DIR / "Vacantes" / "pedidos_cv"  # ⭐ Me interesa desde la app: la API deja {"id": ...} aqui
BUSQUEDA_JSON = DIR / "Vacantes" / "busqueda.json"
CONTACTOS_JSON = DIR / "Correos" / "contactos.json"
ESTADOS = ("nueva", "me_interesa", "aplicada", "descartada")

PROMPT_BUSQUEDA = """Busca en internet VACANTES DE TRABAJO REALES y vigentes para Salvador Pantoja (Chava), de Guadalajara.
Hoy es {hoy}.

Primero lee (Read) estos archivos de tu carpeta para conocerlo:
- Perfil/perfil-software.md y Perfil/perfil-electronica.md (lo que contó en su entrevista; pueden estar cortos)
- Perfil/cv_actual.md y Perfil/proyectos.md

Tiene DOS perfiles y busca de los dos:
1. software / tech: becario, practicante, intern, trainee o junior en desarrollo, QA, soporte TI, redes,
   DevOps/infraestructura, datos. Poca experiencia formal pero proyectos reales (ver proyectos.md).
2. electrónica: técnico de pruebas, diagnóstico, reparación, test engineer, ICT/FCT, calidad, en manufactura
   electrónica (ya trabajó en Foxconn y Plexus). Quiere ver si hay algo MEJOR que lo que tiene.

PRIORIDAD (respétala al elegir qué buscar y qué incluir):
1. Guadalajara/Zapopan/Tlaquepaque/El Salto presencial o híbrido, en ESPAÑOL. Su inglés hablado es flojo.
   Muy bien si el ambiente es bilingüe y ahí podría aprender inglés, pero que NO pidan inglés fluido para entrar.
2. Remoto en México, en español.
3. Remoto internacional SOLO si no exige inglés conversacional/fluido (pocas).
Ojo con el horario: puede seguir estudiando; nota si el horario es flexible o de medio tiempo.

Dónde buscar (Indeed, Computrabajo y Glassdoor suelen bloquear a robots con 403: si uno falla, NO insistas,
pasa al siguiente):
- Portales de empleo de las empresas (casi todos abren): Workday de Flex (flextronics.wd1.myworkdayjobs.com), Jabil
  (jabil.wd5.myworkdayjobs.com), Intel, Sanmina, Continental, Bosch, HP; careers de Oracle, IBM, Siemens, Honeywell,
  Celestica, Benchmark, Kimball, Plexus, Foxconn; Wizeline, Softtek, Globant, EPAM, Kueski y startups de GDL.
- Get on Board (getonbrd.com, en español, tiene remoto LatAm), OCC Mundial, LinkedIn Jobs (páginas públicas
  linkedin.com/jobs/view/...), bolsas del CETI, UdeG/CUCEI, ITESO y Tec GDL, programas de becarios/trainee.
- Usa WebSearch con consultas concretas, por ejemplo: "becario sistemas Zapopan", "practicante soporte TI
  Guadalajara", "trainee desarrollador Guadalajara 2026", "técnico de pruebas electrónica Guadalajara",
  "test technician Guadalajara" y lo mismo con site:linkedin.com/jobs, site:getonbrd.com, site:occ.com.mx.
Verifica abriendo la página. Si la página no abre (403) pero la vacante salió en el buscador con su link real y
se ve vigente, inclúyela con "verificada": false. Si abriste la página y sigue abierta: "verificada": true.
META: al menos 10 vacantes (mezcla de los dos perfiles). Usa al menos 20 búsquedas antes de rendirte.

Vacantes que ya conozco (no las repitas salvo que tengas un link mejor):
{conocidas}

CORREOS PARA MANDAR CV: busca también correos PUBLICADOS para recibir CVs: en vacantes, páginas de "Trabaja con
nosotros", bolsas de universidades y publicaciones de reclutadores. Consultas útiles: "envía tu CV" becario
Guadalajara, "manda tu CV a" practicantes Zapopan, "reclutamiento@" Guadalajara técnico, "rh@" Guadalajara sistemas,
"bolsa de trabajo" CUCEI correo. Intenta juntar al menos 5. NUNCA inventes ni adivines correos (nada de
nombre.apellido@empresa): solo los que aparecen escritos tal cual en una página pública (pon esa página en "fuente").

PRESUPUESTO: trabaja tú solo, sin sub-agentes ni skills. Hasta ~30 búsquedas y ~25 páginas. No inventes nada:
sin un link real (del buscador o de la página), no va.

Escribe el resultado en Vacantes/busqueda.json (sobrescríbelo) con este formato exacto:
{{"generado": "{hoy}", "vacantes": [{{
  "puesto": "...", "empresa": "...", "perfil": "software|electronica",
  "modalidad": "Presencial|Híbrido|Remoto", "zona": "colonia/municipio o 'Remoto MX'",
  "prioridad": 1|2|3, "ingles": "no|lectura|conversacion|fluido", "aprende_ingles": true|false,
  "sueldo": "$… al mes o 'Sin dato'", "horario": "… (¿compatible con estudiar?)",
  "requisitos": "lo principal en 1 línea", "link": "https://...", "publicada": "AAAA-MM-DD o ''",
  "match": 1-5, "verificada": true|false, "por_que": "1-2 líneas: por qué le queda (o qué le falta)"}}],
 "contactos": [{{"empresa": "...", "correo": "...", "perfil": "software|electronica",
  "fuente": "https://página donde aparece", "nota": "qué piden (ej. 'mandar CV para prácticas')"}}]}}
No crees ni modifiques ningún otro archivo. Tu respuesta final: cuántas vacantes y contactos encontraste, en una línea."""

REGLAS_CV = "CVs/Generadores/REGLAS_CV.md"
PROMPT_CV = """Arma un CV A LA MEDIDA de Salvador Pantoja (Chava) para esta vacante.

Primero lee y sigue AL PIE DE LA LETRA las reglas de CVs/Generadores/REGLAS_CV.md: dicen de dónde sacar los datos
(entrevista en Perfil/, specs maestros, cv_actual.md), qué se vale y qué no, y cómo generarlo SIEMPRE con
CVs/Generadores/cv_builder.py para que salga con su mismo diseño en .docx y .pdf de 1 página.

La vacante:
{vacante}"""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def vacante_id(v: dict) -> str:
    link = re.sub(r"[?#].*$", "", (v.get("link") or "").strip().lower())
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{_norm(v.get('empresa', ''))}|{_norm(v.get('puesto', ''))}|{link}").hex[:10]


def _leer(ruta: Path) -> list[dict]:
    try:
        d = json.loads(ruta.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _escribir(ruta: Path, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, ruta)
    finally:
        Path(tmp).unlink(missing_ok=True)


def cargar() -> list[dict]:
    return _leer(VACANTES_JSON)


def guardar(vs: list[dict]):
    _escribir(VACANTES_JSON, vs)


def prompt_busqueda(hoy: str) -> str:
    conocidas = [f"- {v.get('puesto')} — {v.get('empresa')} ({v.get('link', '')})" for v in cargar()
                 if v.get("estado") != "descartada"][-60:]
    return PROMPT_BUSQUEDA.format(hoy=hoy, conocidas="\n".join(conocidas) or "(ninguna)")


def leer_busqueda() -> tuple[list[dict], list[dict]] | None:
    """(vacantes, contactos) que dejo Claude, o None si no dejo nada legible."""
    try:
        d = json.loads(BUSQUEDA_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return list(d.get("vacantes") or []), list(d.get("contactos") or [])


def _limpia(v: dict) -> dict | None:
    if not v.get("puesto") or not v.get("empresa") or not str(v.get("link", "")).startswith("http"):
        return None
    v = {k: v.get(k) for k in ("puesto", "empresa", "perfil", "modalidad", "zona", "prioridad", "ingles", "aprende_ingles",
                               "sueldo", "horario", "requisitos", "link", "publicada", "match", "verificada", "por_que")}
    v["perfil"] = v["perfil"] if v["perfil"] in ("software", "electronica") else "software"
    v["ingles"] = v["ingles"] if v["ingles"] in ("no", "lectura", "conversacion", "fluido") else "no"
    try:
        v["match"] = max(1, min(5, int(v["match"])))
    except (TypeError, ValueError):
        v["match"] = 3
    try:
        v["prioridad"] = max(1, min(3, int(v["prioridad"])))
    except (TypeError, ValueError):
        v["prioridad"] = 2
    v["aprende_ingles"] = bool(v["aprende_ingles"])
    v["verificada"] = v["verificada"] is not False  # sin dato = se asume verificada (busquedas viejas)
    return v


def integrar(encontradas: list[dict], hoy: str, avisado: bool) -> list[dict]:
    """Mete lo nuevo a vacantes.json sin perder estados. Devuelve solo las nuevas."""
    todas = {v["id"]: v for v in cargar() if v.get("id")}
    nuevas = []
    for crudo in encontradas:
        v = _limpia(crudo)
        if not v:
            continue
        v["id"] = vacante_id(v)
        if v["id"] in todas:
            viejo = todas[v["id"]]
            todas[v["id"]] = viejo | {k: x for k, x in v.items() if x not in (None, "")} | {
                "estado": viejo.get("estado", "nueva"), "encontrada": viejo.get("encontrada", hoy)}
        else:
            v |= {"estado": "nueva", "encontrada": hoy, "avisado": avisado}
            todas[v["id"]] = v
            nuevas.append(v)
    guardar(list(todas.values()))
    return ordenar(nuevas)


def integrar_contactos(contactos: list[dict], hoy: str) -> int:
    """Correos publicados para mandar CV (los usa el lote diario de correos). Devuelve cuantos son nuevos."""
    actuales = _leer(CONTACTOS_JSON)
    vistos = {c.get("correo", "").lower() for c in actuales}
    nuevos = 0
    for c in contactos:
        correo = str(c.get("correo", "")).strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", correo) or correo in vistos or not str(c.get("fuente", "")).startswith("http"):
            continue
        actuales.append({"correo": correo, "empresa": str(c.get("empresa", "")).strip(),
                         "perfil": c.get("perfil") if c.get("perfil") in ("software", "electronica") else "software",
                         "fuente": c["fuente"], "nota": str(c.get("nota", "")).strip(), "encontrado": hoy,
                         "estado": "pendiente"})
        vistos.add(correo)
        nuevos += 1
    if nuevos:
        _escribir(CONTACTOS_JSON, actuales)
    return nuevos


def ordenar(vs: list[dict]) -> list[dict]:
    return sorted(vs, key=lambda v: (v.get("prioridad", 2), -v.get("match", 3)))


def activas() -> list[dict]:
    return ordenar([v for v in cargar() if v.get("estado") in ("nueva", "me_interesa")])


def _ahora() -> str:
    return jarvis.ahora().isoformat(timespec="seconds")


def poner_estado(vid: str, estado: str, **extra) -> dict | None:
    """Cambia el estado de una vacante. Al marcarla `aplicada` guarda la fecha (la primera vez)."""
    vs = cargar()
    v = next((x for x in vs if x.get("id") == vid), None)
    if v:
        v["estado"] = estado
        if estado == "aplicada" and not v.get("fecha_aplicada"):
            v["fecha_aplicada"] = _ahora()
        v.update(extra)
        guardar(vs)
    return v


def carpeta_cv(v: dict) -> str:
    return "Electronica" if v.get("perfil") == "electronica" else "Software"


def nombre_cv(v: dict) -> str:
    """Nombre FIJO del CV a la medida de una vacante (sin extension): asi la app y el recordatorio saben si ya esta."""
    base = re.sub(r"\(.*?\)", "", f"{v.get('empresa', '')}_{v.get('puesto', '')}")
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    return "Salvador_Pantoja_CV_" + re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:60]


def cv_listo(v: dict) -> Path | None:
    """El PDF del CV a la medida de esa vacante, si ya existe."""
    if not v.get("cv_archivo"):
        return None
    return next(iter(sorted((DIR / "CVs" / v.get("cv_carpeta", carpeta_cv(v))).glob(f"{v['cv_archivo']}*.pdf"))), None)


def pedir_cv(vid: str) -> dict | None:
    """\"Me interesa\": la marca y anota que su CV a la medida va en camino (nombre fijo)."""
    vs = cargar()
    v = next((x for x in vs if x.get("id") == vid), None)
    if not v:
        return None
    return poner_estado(vid, "aplicada" if v.get("estado") == "aplicada" else "me_interesa",
                        cv_archivo=nombre_cv(v), cv_carpeta=carpeta_cv(v), cv_pedido=_ahora())


def para_recordar(horas: int = 24) -> list[dict]:
    """Vacantes con CV listo hace mas de `horas` y sin respuesta de Chava (ni aplicada ni descartada), sin recordar aun."""
    limite = jarvis.ahora().timestamp() - horas * 3600
    out = []
    for v in cargar():
        if v.get("estado") != "me_interesa" or v.get("recordado") or not v.get("cv_pedido"):
            continue
        pdf = cv_listo(v)
        if pdf and pdf.stat().st_mtime < limite:
            out.append(v)
    return out


INGLES = {"no": "sin inglés", "lectura": "inglés solo leer", "conversacion": "⚠️ inglés conversación", "fluido": "⚠️ inglés fluido"}


def linea(i: int, v: dict) -> str:
    e = html.escape
    extras = " · ".join(x for x in (v.get("modalidad"), v.get("zona"), INGLES.get(v.get("ingles", "")),
                                    "aprendes inglés ✨" if v.get("aprende_ingles") else "", v.get("sueldo"),
                                    "🔎 por confirmar" if v.get("verificada") is False else "") if x)
    estado = {"me_interesa": " ⭐", "aplicada": " ✅"}.get(v.get("estado", ""), "")
    return (f"<b>{i}. {e(v['puesto'])}</b> — {e(v['empresa'])}{estado}\n"
            f"{'★' * v.get('match', 3)}{'☆' * (5 - v.get('match', 3))} · {'💻' if v.get('perfil') == 'software' else '🔧'} {e(extras)}\n"
            f"{e((v.get('por_que') or '')[:220])}\n<a href=\"{e(v.get('link', ''), quote=True)}\">ver vacante</a>")


POR_PAGINA = 8


def teclado(vs: list[dict], inicio: int = 1, pagina: int | None = None, paginas: int = 1) -> InlineKeyboardMarkup | None:
    """Por vacante: [⭐ Me interesa → CV] [🙅]. Con `pagina`, al final ‹ Anteriores · Más ›."""
    filas = [[InlineKeyboardButton(f"⭐ {i} Me interesa → CV", callback_data=f"emp:int:{v['id']}"),
              InlineKeyboardButton(f"🙅 {i}", callback_data=f"emp:no:{v['id']}")]
             for i, v in enumerate(vs, inicio) if v.get("estado") != "aplicada"]
    if pagina is not None and paginas > 1:
        nav = []
        if pagina > 0:
            nav.append(InlineKeyboardButton("‹ Anteriores", callback_data=f"emp:pag:{pagina - 1}"))
        if pagina < paginas - 1:
            nav.append(InlineKeyboardButton("Más ›", callback_data=f"emp:pag:{pagina + 1}"))
        filas.append(nav)
    return InlineKeyboardMarkup(filas) if filas else None


def texto_lista(vs: list[dict], titulo: str, inicio: int = 1) -> str:
    cuerpo = "\n\n".join(linea(i, v) for i, v in enumerate(vs, inicio))
    return (f"<b>{titulo}</b>\n\n{cuerpo}\n\n⭐ Me interesa = te armo un CV a la medida y te lo mando para que apliques.\n"
            "Cuando apliques, márcalo en Telegram o en tu app (pestaña Jobs).")[:4000]


def pagina_puestos(pagina: int = 0) -> tuple[str, InlineKeyboardMarkup | None, int]:
    """(texto, teclado, total) de /puestos: todas las activas, POR_PAGINA a la vez."""
    vs = activas()
    paginas = max(1, -(-len(vs) // POR_PAGINA))
    pagina = max(0, min(pagina, paginas - 1))
    trozo = vs[pagina * POR_PAGINA:(pagina + 1) * POR_PAGINA]
    inicio = pagina * POR_PAGINA + 1
    titulo = f"💼 Puestos para ti ({len(vs)} activos · página {pagina + 1} de {paginas})"
    return texto_lista(trozo, titulo, inicio), teclado(trozo, inicio, pagina, paginas), len(vs)


def prompt_cv(v: dict) -> str:
    """CV a la medida para una vacante de vacantes.json (boton ⭐ Me interesa). El nombre es fijo (nombre_cv)."""
    datos = {k: v.get(k) for k in ("puesto", "empresa", "perfil", "modalidad", "zona", "requisitos", "horario", "ingles", "link", "por_que")}
    if v.get("detalles"):  # lo que pegó con /cv: la vacante completa, tal cual
        datos["detalles"] = v["detalles"]
    return PROMPT_CV.format(vacante=json.dumps(datos, ensure_ascii=False, indent=1)) + (
        f"\n\nNOMBRE Y CARPETA OBLIGATORIOS (la app los usa para saber que ya está): spec "
        f"CVs/Generadores/specs/{nombre_cv(v)}.json con \"archivo\": \"{nombre_cv(v)}\" y \"carpeta\": \"../{carpeta_cv(v)}\". "
        "Si ya existen, sobrescríbelos (es el mismo puesto, se rehace).")


def prompt_cv_texto(texto: str) -> str:
    """CV a la medida con lo que Chava pego de la vacante (/cv <detalles>)."""
    return PROMPT_CV.format(vacante=texto.strip()[:6000])


def lista_cvs(maximo: int = 10) -> list[Path]:
    """Los dos maestros primero y luego los mas nuevos de Software/ y Electronica/ (PDF)."""
    cvs = DIR / "CVs"
    maestros = [cvs / "00_Maestros" / f"Salvador_Pantoja_CV_{p}.pdf" for p in ("Software", "Electronica")]
    otros = sorted((f for c in ("Software", "Electronica") for f in (cvs / c).glob("*.pdf")),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return ([m for m in maestros if m.exists()] + otros)[:maximo]


def buscar_vacantes(texto: str) -> list[dict]:
    """Vacantes que coinciden con lo que dijo Chava (empresa y/o puesto, sin acentos). Todas las palabras de 3+
    letras deben aparecer en "empresa puesto"."""
    palabras = [w for w in _norm(texto).split() if len(w) >= 3 and w not in ("vacante", "la", "las", "del", "que", "para")]
    if not palabras:
        return []
    return [v for v in cargar() if all(w in _norm(f"{v.get('empresa', '')} {v.get('puesto', '')}") for w in palabras)]


def pedidos_cv() -> list[tuple]:
    """(ruta, {"id": ...}) de los ⭐ que le dio en la app, los mas viejos primero. Los rotos se borran."""
    out = []
    for ruta in sorted(PEDIDOS_CV.glob("*.json")) if PEDIDOS_CV.is_dir() else []:
        try:
            out.append((ruta, json.loads(ruta.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            ruta.unlink(missing_ok=True)
    return out


def vacante_de_cv(datos: dict, texto: str) -> dict:
    """/cv <detalles>: la vacante que pegó Chava entra a vacantes.json como ⭐ me_interesa, para que su CV salga en
    la app en «Listos para aplicar». `datos` = lo que se sacó del texto (puesto, empresa, link, perfil...). Si ya
    estaba (misma empresa y puesto, o mismo link) se reutiliza esa."""
    puesto = str(datos.get("puesto") or "").strip()[:80] or "Puesto sin nombre"
    empresa = str(datos.get("empresa") or "").strip()[:60] or "Empresa sin nombre"
    link = str(datos.get("link") or "").strip()
    link = link if link.startswith("http") else ""
    vs = cargar()
    clave = (_norm(empresa), _norm(puesto))
    v = next((x for x in vs if (_norm(x.get("empresa", "")), _norm(x.get("puesto", ""))) == clave
              or (link and x.get("link") == link)), None)
    if v is None:
        v = {"puesto": puesto, "empresa": empresa, "link": link,
             "perfil": datos.get("perfil") if datos.get("perfil") in ("software", "electronica") else "software",
             "modalidad": str(datos.get("modalidad") or ""), "zona": str(datos.get("zona") or ""),
             "ingles": datos.get("ingles") if datos.get("ingles") in ("no", "lectura", "conversacion", "fluido") else "no",
             "sueldo": str(datos.get("sueldo") or ""), "match": 3, "prioridad": 2, "aprende_ingles": False,
             "verificada": True, "por_que": "La pegaste tú con /cv.", "origen": "cv"}
        v["id"] = vacante_id(v)
        v |= {"estado": "me_interesa", "encontrada": _ahora()[:10], "avisado": True}
        vs.append(v)
    elif v.get("estado") in ("nueva", "descartada"):
        v["estado"] = "me_interesa"
    v["detalles"] = texto.strip()[:6000]
    guardar(vs)
    return v
