---
nombre: sesion-pc
cuando: Chava quiere usar él mismo Claude Code en una carpeta de la Pi desde la app de Claude del iPhone ("ábreme Claude en…", "quiero trabajar yo en…", "sesión de Claude Code", "abre una sesión en la carpeta de Cultura").
---
# Sesiones de Claude Code para su iPhone

Es distinto de encargarle un trabajo a Claude (`delegar_a_claude`): aquí **Chava maneja la sesión él mismo** desde la
app de Claude (pestaña **Code**, punto verde). La sesión vive en la Pi aunque se le caiga el SSH o cierre Termius.

## Cómo
1. `sesion_claude` con `accion: "abrir"`:
   - Tarea o cosa de una materia → `materia` (y `parcial` si lo dice; si no, el parcial actual).
   - Otra carpeta → `carpeta` como se ve en Samba (`Documentos/Creaciones/Videos/…`, `Servicios/asistente`) o un atajo:
     `home` (toda la Pi), `servicios`, `korra`, `personal`, `creaciones`, `escuela`, `asistente`, `dinero`.
   - Si no dice dónde, pregúntale en UNA línea con botones (`ofrecer_opciones`): su materia más probable, "Toda la Pi", "Servicios".
2. Contéstale con lo que regresa la herramienta: nombre de la sesión y el link. Una línea extra: edita sola y le pide
   permiso en el iPhone antes de correr comandos.
3. Máximo 2 abiertas (la Pi tiene 4 GB). Si ya hay 2, dile cuáles y ofrécele cerrar una (`accion: "cerrar"`, `nombre`).

## No
- No la abras para hacerle tú un trabajo: para eso es `delegar_a_claude`.
- No inventes links: solo el que regresa la herramienta.
- Él también puede usar `/pc materia` o `/pc` (ver y cerrar las abiertas).
