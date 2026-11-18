FROM python:3.12-slim
WORKDIR /app
# ffmpeg: convierte la voz de Edge TTS a nota de voz de Telegram (OGG/Opus)
# cups-client: lp/lpstat/cancel contra el CUPS del host · cups-ipp-utils: ipptool para leer tinta y papel
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg cups-client cups-ipp-utils \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY habilidades/ ./habilidades/
# uid/gid 1000 = dueño de CETI COLOMOS en el host (para guardar lo que mandas por Telegram)
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m app && mkdir /data && chown app:app /data
USER app
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
