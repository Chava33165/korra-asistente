# KORRA · asistente personal en Telegram

> *Un asistente que vive en mi Raspberry Pi y me ayuda con la escuela, el trabajo, el dinero y la búsqueda de empleo.*
> *A personal Telegram assistant running on my Raspberry Pi 5 (Python, DeepSeek tool calling, Claude Code workers).*

KORRA entiende lenguaje natural (texto o notas de voz) y decide qué herramienta usar. Lo rápido lo resuelve
ella misma con **DeepSeek + tool calling**; lo pesado (hacer una tarea en Word, armar un CV, investigar) se lo
encarga a **Claude Code**, que corre en un *worker* aparte dentro de un contenedor.

## Lo que hace

- 📅 **Google Calendar, Gmail y Classroom** (OAuth2): "¿qué me falta entregar?", "mándale la tarea al profe".
- 🧠 **Memoria** en SQLite: recuerda lo importante y escribe un diario con una reflexión semanal.
- 🎙️ **Voz**: entiende notas de voz (Whisper) y contesta con voz (TTS).
- 🖨️ **Impresora** por CUPS: "imprime la tarea a color, 2 copias".
- 💼 **Búsqueda de empleo**: encuentra vacantes, arma CVs a la medida con un botón y avisa cuando un reclutador responde.
- 💸 **Dinero**: registra gastos y consulta saldos de mi API de finanzas.
- 🕰️ **Iniciativa propia**: rutina diaria, recordatorios y mensajes cuando hay un motivo real (no spam).

## Cómo está hecho

| Pieza | Qué hace |
|---|---|
| `bot/` | Bot de Telegram (python-telegram-bot). DeepSeek con ~40 herramientas, "habilidades" en Markdown que se cargan según la conversación, memoria y trabajos programados. |
| `worker/` | API FastAPI que ejecuta `claude -p` en una carpeta y devuelve los archivos que generó. |
| `vigilante/` | Revisa que todo siga vivo. |
| `docker-compose.yml` | Contenedores sin root, sistema de archivos de solo lectura, capacidades mínimas y límites de RAM. |

**Stack:** Python 3.12 · python-telegram-bot · DeepSeek (OpenAI SDK) · Claude Code · FastAPI · SQLite · Google APIs · Docker Compose

Las pruebas corren aisladas (`docker run --network none …`) para no tocar los datos reales.

## Instalación

Ver [docs/INSTALACION.md](docs/INSTALACION.md).

---
Hecho por **Salvador Pantoja** · construido con ayuda de IA (Claude) como herramienta, entendiendo cada pieza.
