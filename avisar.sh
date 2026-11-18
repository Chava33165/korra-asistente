#!/usr/bin/env bash
# Manda un archivo (o un texto) a Chava por Telegram usando el bot KORRA (contenedor `bot` de este proyecto).
# Es el único camino para avisos desde el host: lo usan la skill korra-telegram, el respaldo de iCloud y el vigilante.
# Uso: avisar.sh <archivo> [leyenda]     |     avisar.sh --texto "mensaje"
# El archivo se pasa por stdin, así que sirve cualquier ruta del host. Límite de Telegram para bots: 50 MB.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
command -v docker >/dev/null || { echo "Sin docker: este script corre en el host de la Pi, no dentro del worker." >&2; exit 2; }
# Con el bot prendido se usa su contenedor; si está caído (el vigilante avisa justo de eso) se arranca uno desechable
# con la misma imagen y variables.
if [[ -n "$(docker compose ps -q --status running bot 2>/dev/null)" ]]; then
  EN_BOT=(exec -T)
else
  EN_BOT=(run --rm --no-deps -T)
fi

if [[ "${1:-}" == "--texto" ]]; then
  MODO=texto NOMBRE="" LEYENDA="${2:?falta el texto}"; export MODO NOMBRE LEYENDA
  exec docker compose "${EN_BOT[@]}" -e MODO -e NOMBRE -e LEYENDA bot python -c "$(sed -n '/^#PY$/,$p' "$(readlink -f "$0")")" </dev/null
fi

ARCHIVO="${1:?uso: avisar.sh <archivo> [leyenda]}"
[[ -f "$ARCHIVO" ]] || { echo "No existe: $ARCHIVO" >&2; exit 1; }
MB=$(( $(stat -c %s "$ARCHIVO") / 1048576 ))
(( MB < 50 )) || { echo "Pesa ${MB} MB; Telegram solo deja 50 MB a los bots. Pásalo por Samba o iCloud." >&2; exit 1; }
MODO=archivo NOMBRE="$(basename "$ARCHIVO")" LEYENDA="${2:-}"
export MODO NOMBRE LEYENDA
docker compose "${EN_BOT[@]}" -e MODO -e NOMBRE -e LEYENDA bot python -c "$(sed -n '/^#PY$/,$p' "$(readlink -f "$0")")" < "$ARCHIVO"
exit $?

#PY
import asyncio, io, os, sys
from pathlib import Path
from telegram import Bot

async def main():
    chat = int(os.environ["ALLOWED_CHAT_ID"])
    modo, nombre, leyenda = os.environ["MODO"], os.environ["NOMBRE"], os.environ["LEYENDA"][:1000]
    t = {"write_timeout": 300, "read_timeout": 120}
    async with Bot(os.environ["TELEGRAM_TOKEN"]) as b:
        if modo == "texto":
            m = await b.send_message(chat, leyenda)
        else:
            fh = io.BytesIO(sys.stdin.buffer.read())
            ext = Path(nombre).suffix.lower()
            if ext in {".mp4", ".mov", ".m4v"}:
                m = await b.send_video(chat, fh, filename=nombre, caption=leyenda, supports_streaming=True, **t)
            elif ext in {".png", ".jpg", ".jpeg", ".webp"} and fh.getbuffer().nbytes < 10 * 1024 * 1024:
                m = await b.send_photo(chat, fh, caption=leyenda, **t)
            elif ext in {".mp3", ".m4a", ".ogg", ".wav"}:
                m = await b.send_audio(chat, fh, filename=nombre, caption=leyenda, **t)
            else:
                m = await b.send_document(chat, fh, filename=nombre, caption=leyenda, **t)
    print(f"Enviado por Telegram (mensaje {m.message_id})")

asyncio.run(main())
