# KORRA (asistente de Telegram)

Telegram (DeepSeek) → Claude Code → tus carpetas de la Pi, que ves desde la Mac por Samba (`~/Servicios/samba`).

| Servicio | Qué hace | RAM máx. |
|---|---|---|
| `bot` | Bot de Telegram + DeepSeek; decide cuándo llamar a Claude | 384 MB |
| `worker` | Ejecuta `claude -p` con tus skills sobre tus carpetas | 1.2 GB |

Carpetas (en la Pi → dentro de los contenedores; por dentro no cambiaron al reorganizar):

| En la Pi | En los contenedores | Qué es |
|---|---|---|
| `~/Documentos/Escuela` (`CETI_DIR`) | `/workspace` | Escuela (`SEMESTRE_N/<MATERIA>/P1-P3`) |
| `~/KORRA` (`JARVIS_DIR`) | `/jarvis` | Cerebro de KORRA (ver `~/KORRA/LEEME.md`) |
| `~/Documentos/Personal` (`PERSONAL_DIR`) | `/jarvis/archivos` | Archivos personales (nóminas…) |

Las herramientas y el worker siguen llamándolas `CETI COLOMOS/…` y `JARVIS/…`; a Chava se le muestran con su nombre en
Samba (`a_samba()` en `bot/bot.py`).

## 1. Llenar `.env`
```bash
cd ~/Servicios/asistente && nano .env      # ya existe, copiado de .env.example (chmod 600)
```
- **TELEGRAM_TOKEN**: en Telegram abre @BotFather → `/newbot`.
- **ALLOWED_CHAT_ID**: arranca el bot, mándale `/start` y te dice tu id; ponlo y reinicia.
- **DEEPSEEK_API_KEY**: platform.deepseek.com → API keys (hay que recargar saldo).
- **CLAUDE_CODE_OAUTH_TOKEN**: en la Pi corre `claude setup-token` (usa tu Pro/Max).
- **FINANZAS_TOKEN**: el `BEARER_TOKEN` de `~/Servicios/task-hub/.env`, para que el resumen de la mañana hable de dinero.

## 2. Arrancar
```bash
docker compose up -d --build
docker compose logs -f bot worker          # Ctrl+C para salir
docker compose exec worker claude -p "di hola"   # prueba de la autenticación de Claude
```
Hay que levantar este proyecto **antes** que `task-hub` (usa la red `asistente_interna`).

Pruebas de la memoria (base temporal, nunca la real):
```bash
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD/bot":/app:ro -w /app asistente-bot python -m unittest discover -s tests -v
```

## Avisos desde el host, respaldos y vigilante
- **`avisar.sh`**: el único camino para mandarle algo a Chava desde la Pi (`./avisar.sh --texto "…"` o
  `./avisar.sh archivo [leyenda]`). Usa el contenedor `bot` (o uno desechable si está caído), así que nunca lee `.env`.
  Lo usan la skill `korra-telegram`, `~/Servicios/icloud-respaldo/respaldo.sh` y el vigilante.
- **`vigilante/vigilante.py`** (cron cada 10 min): contenedores caídos o unhealthy, disco > 85 %, temperatura > 75 °C,
  swap > 80 % y respaldo de iCloud de hace más de 26 h. Avisa solo cuando algo cambia y deja `vigilante/estado.json`.
  Prueba: `VIGILANTE_PRUEBA=1 python3 vigilante/vigilante.py` fuerza un aviso de disco (la siguiente vuelta normal
  avisa que ya se resolvió).
- El bot lee de solo lectura `estado/icloud.json` del respaldo y el del vigilante (`bot/estado_servidor.py`) para
  `/respaldos`, `/servidor` y el resumen de la mañana.

## Trabajos de Claude: carpeta, entrega y revisión
- `delegar_a_claude` lleva `tipo` (tarea, presentacion, video, diseno, documento, archivos, otro); con `espacio` y
  `materia` decide la carpeta (`destino_job()` en `bot/bot.py`): materia → su parcial (el actual si no dice);
  video/diseño/presentación personal → `~/Documentos/Creaciones/<Tipo>/<fecha título>` (`/creaciones`, `CREACIONES_DIR`);
  papeles → `~/Documentos/Personal`. `/claude` directo adivina tipo y materia (`adivinar_pedido`).
- Video y presentación: primero un plan con [🚀 Hazlo] [✏️ Cambiar] (el pedido espera en `kv`, `pedido:<id>`).
- El worker le pide a Claude escribir `.korra/entrega.json` con los archivos FINALES; si no, filtra lo cambiado.
  `snapshot()` solo recorre la carpeta del trabajo y se salta `node_modules`, `_proyecto/`, `out/`…
- `worker/revisar.py` revisa cada entrega sin usar Claude (páginas, diapositivas, portada y prefijo `26310453_` en la
  escuela, duración/resolución del video) y saca una vista previa (`GET /archivo/<job>_<n>.jpg`).
- Videos de más de 49 MB: `POST /comprimir` hace una copia temporal que cabe en Telegram.
- Los trabajos sobreviven reinicios: `jobs_info` en la base del bot y `/data/jobs.json` en el worker. Errores
  clasificados (`error_tipo`: limite, timeout, interrumpido, fallo); con límite hay botón ⏰ para reintentar en 1 h.
- Pruebas del worker: `docker compose run --rm --no-deps -v ./worker:/app -w /app worker python3 -m unittest discover -s tests -v`

## Sesiones de Claude Code para el iPhone (`/pc`)
`/pc <materia o carpeta>` o la herramienta `sesion_claude` (habilidad `sesion-pc`) piden al lanzador del host
(`~/Servicios/claude-remoto`, buzón montado en `/remoto`) una sesión de `claude --remote-control` en esa carpeta.
Chava la usa desde la app de Claude → Code; no depende de SSH. Ver `~/Servicios/claude-remoto/LEEME.md`.

## Uso desde Telegram
- Escribe normal. Ej: *"Hazme el resumen de materialismo histórico para Cultura Comparada P2,
  1 página, en Word con portada y también en PDF"*.
- Manda fotos o PDFs de la tarea con el pie de foto diciendo qué hacer.
- `/materias` `/semestre` `/estado` `/cancelar [id]` `/olvidar` `/respaldos` `/servidor`
- `/nuevo_semestre Física, Cálculo Integral (Prof. X), … grupo: I-2Me`: crea `SEMESTRE_2`.

## Notas
- Las reglas que sigue Claude están en `~/Documentos/Escuela/CLAUDE.md` y `~/KORRA/CLAUDE.md`.
- Los logs de cada trabajo quedan en el volumen `worker-data` (`/data/logs`).
- El worker corre con `bypassPermissions`: solo ve tus carpetas y su propio home, sin root.
  `CLAUDE.md` le prohíbe borrar o sobrescribir archivos.

## KORRA: personalidad, perfil, memoria e iniciativa
Su espacio propio es `~/KORRA`; qué es cada archivo está en `~/KORRA/LEEME.md`.

Rutina diaria (horas en `.env`: `HORA_MANANA`, `HORA_NOCHE`, `HORA_REFLEXION`, `HORA_REFLEXION_SEMANAL`; vacío = apagado):
- **08:00**: te escribe primero: agenda, tareas, correos, dinero, respaldos y servidor (y los domingos, la semana).
- **22:00**: check-in de cómo te fue (si no hay nada que decir, no escribe).
- **03:30**: relee la plática del día, escribe su diario y actualiza su memoria (recupera los días que fallaron).
- **Domingo 04:00**: reflexión de la semana: reescribe `perfil.md` y deja el resumen de la semana.

Comandos: `/memoria`, `/notas [nombre]`, `/recordatorios`, `/olvidar` (borra la plática, no la memoria).
Tras cambiar `.env`: `docker compose up -d bot`.

## Voz y botones
- **Notas de voz**: se transcriben con Whisper en Groq (`GROQ_API_KEY`, console.groq.com). Te
  responde con nota de voz (Edge TTS, `TTS_VOZ`) + el texto.
- **Botones de Jarvis**: él decide cuándo ofrecer opciones (herramienta `ofrecer_opciones`).
- **Tareas de Claude**: `[🛑 Cancelar]` mientras trabaja; `[🔁 Rehacer] [✏️ Corregir algo]` al terminar.

## Google: Calendar, Gmail y Classroom
### 1. Crear las "apps" en Google Cloud (dos proyectos, uno por cuenta)
**A) Escuela (Classroom + correo/calendario del CETI)**: proyecto `KORRA` dentro de la organización ceti.mx.
1. APIs y servicios → Biblioteca → habilita *Google Calendar API*, *Gmail API*, *Google Classroom API*, *Google Drive API*.
2. Google Auth Platform → Branding: nombre `KORRA`, tu correo. Público → tipo **Interno**
   (solo cuentas @ceti.mx; no pide verificación ni caduca).
3. Clientes → Crear cliente → **App de escritorio** → descarga el JSON →
   guárdalo como `~/Servicios/asistente/secrets/google_client_escuela.json`.

**B) Personal (Gmail + Calendar personal)**: entra a console.cloud.google.com con tu **Gmail personal**
y crea otro proyecto `KORRA-personal` (sin organización).
1. Habilita *Google Calendar API* y *Gmail API*.
2. Branding como arriba. Público → tipo **Externo**, agrega tu Gmail como usuario de prueba y luego
   **Publicar app → En producción** (si queda en "Prueba", Google corta el acceso cada 7 días).
3. Cliente **App de escritorio** → JSON → `~/Servicios/asistente/secrets/google_client_personal.json`.

### 2. Conectar las cuentas (desde Telegram)
- `/google escuela` → abre el link con tu cuenta del CETI, acepta, y pega en el chat la URL
  `http://localhost:8765/?code=…` a la que te manda (la página no carga; es normal).
- `/google personal` → lo mismo con tu Gmail.
- `/google` sin nada → estado de las cuentas.

Si al conectar la cuenta del CETI sale "Acceso bloqueado / tu administrador…", la escuela no deja
usar apps externas. En ese caso Classroom no se puede conectar así; Calendar y Gmail personal sí.

### Qué hace
- **Calendar**: "el lunes dentista a las 10" → lo agenda solo. En la mañana te arma el itinerario
  (agenda + tareas + correos) y en la noche te anticipa lo de mañana.
- **Gmail**: busca, lee y resume. Para enviar te muestra el borrador con `[📤 Enviar] [❌ Cancelar]`.
- **Classroom** (cada 15 min): tarea nueva → aviso con botones, evento en el calendario, adjuntos en
  `SEMESTRE_N/MATERIA/<parcial>/<tarea>/` con `instrucciones.md`. Te insiste un día antes (18:00) y el
  día de entrega (8, 13, 17 h y 2 h antes). Si en Classroom ya aparece entregada, deja de insistir.
- `/tareas` pendientes con botones · `/parcial 2` cambia el parcial donde se guardan las tareas.

## Memoria de KORRA (SQLite, en capas)
Todo vive en `/data/bot.db` (volumen `bot-data`). Ver `bot/basedatos.py` y `bot/memoria.py`.
Patrones tomados de agentes personales populares (OpenClaw, Letta/MemGPT, mem0, Generative Agents), sin servicios nuevos:
0. **Perfil** (`~/KORRA/perfil.md`, tope de 3,000 caracteres): quién es Chava, en cada mensaje. Lo reescribe la reflexión
   semanal; la versión anterior queda en `~/KORRA/_archivo/`.
1. **Núcleo** (importancia ★★★ + pendientes vigentes, hasta 5 KB): va en cada mensaje, **repartido por turnos entre
   categorías** para que ninguna se quede fuera (`ORDEN_NUCLEO`). Con perfil, la categoría `perfil` no se repite.
2. **Recuerdos** (tabla `recuerdos`: categoría, importancia, `vigente_hasta`, estado, JSON `extra`): KORRA los
   guarda con `recordar` (si ya hay uno parecido en cualquier categoría, lo actualiza) y los busca con
   `recuerdos_buscar`. Además, con cada mensaje entran solos hasta 5 **recuerdos que vienen al caso** (`relevantes`,
   FTS5). Nada se borra: `olvidar`/caducar cambian el estado y cada cambio de texto queda en `extra.historial`.
3. **Pláticas y diario** con buscador FTS5 (sin importar acentos): `platicas_buscar` busca en TODO el historial.
- **Reflexión nocturna** (3:30): por cada dato decide nuevo/actualizar/olvidar, une duplicados y recupera los días
  que se quedaron sin diario; nunca olvida en masa.
- El prompt pone primero lo que casi no cambia y al final la hora y la agenda, para aprovechar el caché de DeepSeek.
- **`~/KORRA/memoria.md`** es una vista que se regenera sola. Si la editas en la Pi, en ≤5 min se aplica:
  cambiar texto = corregir, borrar línea = olvidar, `- algo` nuevo = recordar. No cambies los `[#número]`.
- `/memoria` (núcleo + categorías con botones) · `/memoria palabra` (buscar).

### Cambiar la base en el futuro (escalable)
Agrega una función al FINAL de `MIGRACIONES` en `bot/basedatos.py` (nueva tabla, columna, índice…). Al arrancar se
aplica sola, con respaldo previo en `/data/respaldos/` (últimos 10). Para atributos nuevos no hace falta columna:
usa la columna JSON `extra`.

## Iniciativa de KORRA
Cada 30 min (`job_pulso` en `bot/bot.py`) revisa si hay un **motivo real** para escribirte, en este orden:
seguimientos programados → exámenes en ≤3 días → eventos de software de la semana sin agendar → ánimo (diario de
ayer) → >20 h sin escribirle → una pregunta de curiosidad al día (en lo que menos sabe de ti). Sin motivo no escribe
(ni gasta DeepSeek). Nunca escribe dormido (`HORAS_DORMIR`, default 22:00-07:00), en el trabajo (`HORARIO_TRABAJO`,
default `lu-sa 06:00-14:00`) ni en clase (calendario 🏫), ni si acaban de platicar.
- `/iniciativa` off · baja (1/día) · **media (3/día, default)** · alta (5/día). `off` también apaga mañana/noche.
- `/silencio 3` → 3 h sin mensajes por iniciativa (recordatorios y entregas sí llegan) · `/silencio 0` lo quita.
- **Seguimientos**: KORRA los programa sola (herramienta `seguimiento`) cuando le cuentas algo que va a pasar, y la
  reflexión nocturna también los detecta. Se guardan como recuerdos de categoría `seguimiento` (`extra.preguntar`).

## Límite de Claude: qué lo gasta y cómo se cuida
El plan Pro se mide por **ventanas de 5 horas** (empiezan con el primer uso) y por semana. `/uso` muestra las dos
barras y, por trabajo, el costo equivalente (entre más alto, más límite se comió).
- Una tarea normal: $0.05–$1.25. La **búsqueda de eventos** con sub-agentes llegó a $5–$9 por corrida → por eso
  ahora corre con `TRABAJO_BARATO` (Haiku, máx. 30 pasos, sin Task/Agent/Skill), solo `DIAS_EVENTOS` (lu,ju) a las
  `HORA_EVENTOS` (01:00, para que la ventana se renueve antes de las 7) y se salta si la ventana ya va >50 %.
  Los eventos nuevos se mandan a las `HORA_AVISO_EVENTOS` (08:10).
- El worker acepta por trabajo `model`, `max_turns` y `disallowed_tools` (ver `JobIn` en `worker/worker.py`):
  úsalo para cualquier automatización futura que corra sola.
- Lo demás (pulso de iniciativa, Classroom, anuncios, memoria) usa DeepSeek, no Claude.

## Impresora de la casa (HP Smart Tank 210-220)
El bot imprime con el CUPS de la Pi (cola `HP_Smart_Tank`) a través de su socket (`/run/cups` montado en el
contenedor): CUPS no se abre a la red. Ver `bot/impresora.py`.
- `/print on` · `/print off` (apagada por default; apagada nadie imprime) · `/print` = estado, tinta y papel.
- Imprimir: archivo con el pie `/print`, responder a un archivo con `/print`, el botón 🖨 bajo lo que entrega
  Claude, o pedírselo a KORRA ("imprime la tarea de Discretas a color"). Siempre sale un diálogo con botones
  (color/B-N, copias, calidad, tamaño) y hay que tocar **🖨 IMPRIMIR**: KORRA nunca imprime sola.
- PDF, imágenes y texto van directo; Word/PowerPoint/Excel se convierten a PDF en el worker (`POST /pdf`,
  LibreOffice, sin Claude). La impresora solo imprime a una cara.
- `IMPRESORA_IP` (.env) solo sirve para leer tinta/papel; si el módem le cambia la IP, actualízala (o fíjala en el módem).

## Limpieza de RAM
`/limpiar` (o `/cleanram`, `/ram`): olvida botones y trabajos viejos (quedan los últimos 30 de cada tipo), le
devuelve al sistema la RAM que Python ya no usa y le pide lo mismo al worker (`POST /limpiar`), que además mata
procesos atorados (LibreOffice, hijos de Claude) si no hay ningún trabajo corriendo. No reinicia nada ni toca la
memoria de KORRA. Corre solo a las `HORA_LIMPIEZA` (04:30; vacío = apagado).
- No puede vaciar la swap ni la caché de disco de la Pi: eso pide root y los contenedores no lo tienen.

## Skills y plugins globales, sesiones de Claude
El worker usa el Claude Code **global de la Pi** (`CLAUDE_GLOBAL_DIR`, `AGENTS_DIR` en `.env`):
- `~/.claude/skills` y `~/.agents` van montados con **lectura y escritura**: el Claude de Telegram ve las mismas skills
  que el de la terminal, y lo que se instala desde Telegram queda global para los dos.
- `~/.claude/plugins` va montado **solo lectura**; el worker carga cada plugin instalado con `--plugin-dir`.
- Lo demás de `~/.claude` (credenciales, ajustes, sesiones de la terminal) **no** se monta: el worker conserva su
  propio home (volumen `claude-home`) y su propio token.
- Las carpetas vacías `docx`, `pdf`, `pptx`, `xlsx` y `deep-research` en `~/.claude/skills` son los puntos de montaje
  de las skills de documentos (`SKILLS_DIR`): no las borres.
- La imagen del worker trae Chromium y ffmpeg para las skills de video (Remotion, HyperFrames).

Comandos:
- `/skills` lista skills y plugins · `/skills owner/repo` o `/claude npx skills add owner/repo` las instala global
  (`POST /skills` del worker corre `npx skills add … -g -y -a claude-code`; no gasta límite de Claude).
- `/sesiones` muestra los trabajos pasados de Claude; tocas uno y tu siguiente mensaje se le manda a esa sesión
  (`claude --resume`, en la carpeta donde se creó). `/continuar <texto>` sigue la última. Cada trabajo terminado
  trae el botón **➡️ Continuar esta sesión**, y KORRA puede hacerlo sola (`continuar_sesion` en `delegar_a_claude`).
- Solo se ven las sesiones del worker (las lanzadas desde Telegram), no las de la terminal o VS Code.

Ojo: cada skill global agrega su descripción al contexto de cada trabajo, así que entre más skills, más límite gasta
cada tarea. Y una skill es texto que Claude obedece: instala solo de repositorios en los que confíes.

## Habilidades de KORRA (sus skills)
El prompt fijo solo trae el núcleo (contexto, cómo trabaja, cómo platica por Telegram, memoria) y un **índice** de
habilidades: una línea por habilidad que dice *cuándo* abrirla. El manual completo lo carga KORRA con la herramienta
`habilidad` solo cuando lo necesita (`bot/habilidades.py`).
- De fábrica: `bot/habilidades/*.md` (calendario, correo, classroom, tareas-escolares, encargar-a-claude, diseno,
  archivos-personales, impresora, estudiar, organizar-dia). Formato: frontmatter `nombre` + `cuando`, luego pasos con
  su criterio de "terminado".
- Propias: `~/KORRA/habilidades/*.md` (se ven por Samba). KORRA las crea con `habilidad_escribir` cuando le enseñas cómo
  quieres algo ("de ahora en adelante…"); con el mismo nombre que una de fábrica, la reemplazan. Guarda `.bak`.
- `/habilidades` lista · `/habilidades nombre` muestra una. Para agregar una de fábrica: un `.md` nuevo y rebuild del bot.

## Empleo (vacantes, CVs y correos a reclutadores)
Datos en `~/Documentos/Personal/Empleo` (= `/jarvis/archivos/Empleo`): `Perfil/` (de la entrevista con IA de la
página Empleo, `~/Servicios/empleo`), `Vacantes/`, `CVs/` y `Correos/`.
- `empleos.py`: búsqueda de vacantes de software y electrónica (lunes y jueves 2:00, aviso 8:20; `/empleos buscar`)
  con botones ⭐ / 🙅 / 📄 CV a la medida. También junta correos publicados para mandar CV.
- `correos_empleo.py`: lote diario (lu-vi 9:30) de correos personalizados con CV adjunto desde la cuenta Google
  `empleo` (`/google empleo`); se aprueba con un botón (`/lote`). Calentamiento 5 → 10 → `MAX_CORREOS_DIA`,
  sin repetir empresa en 60 días, un seguimiento a los 7 días; respuestas y rebotes cada 2 h.
- Variables: `HORA_EMPLEOS`, `DIAS_EMPLEOS`, `HORA_AVISO_EMPLEOS`, `MODELO_EMPLEOS`, `HORA_LOTE_CORREOS`,
  `DIAS_LOTE_CORREOS`, `MAX_CORREOS_DIA`, `CUENTA_EMPLEO`.
