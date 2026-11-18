"""
Voz: notas de voz de Telegram ⇄ texto.

- Escuchar: Whisper en Groq (API compatible con OpenAI). Telegram manda OGG/Opus, que Groq acepta tal cual.
- Hablar: Edge TTS (voz neuronal de Microsoft, gratis) → MP3 → ffmpeg → OGG/Opus, que es
  el formato que Telegram muestra como nota de voz.
"""
import asyncio
import os
import re

import edge_tts
from openai import AsyncOpenAI

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
TTS_VOZ = os.environ.get("TTS_VOZ", "es-MX-DaliaNeural").split("=")[-1].strip() or "es-MX-DaliaNeural"
TTS_VELOCIDAD = os.environ.get("TTS_VELOCIDAD", "+8%")
MAX_TTS_CHARS = 1500

groq = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_API_KEY else None


async def transcribir(audio: bytes, nombre: str = "audio.ogg") -> str:
    if not groq:
        raise RuntimeError("Falta GROQ_API_KEY en .env")
    r = await groq.audio.transcriptions.create(
        model=GROQ_MODEL, file=(nombre, audio), language="es",
        prompt=f"Plática casual en español de México con {os.environ.get('ASISTENTE_NOMBRE', 'KORRA')}: we, wey, neta, qué pedo, CETI, tarea, parcial.",
    )
    return r.text.strip()


def _limpiar(texto: str) -> str:
    """Quita markdown, links y emojis para que no los lea en voz alta."""
    t = re.sub(r"https?://\S+", "el link", texto)
    t = re.sub(r"[*_`#>\[\]]", "", t)
    t = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️]", "", t)
    t = re.sub(r"\n{2,}", ". ", t)
    return t.strip()[:MAX_TTS_CHARS]


async def hablar(texto: str) -> bytes | None:
    """Devuelve la nota de voz en OGG/Opus, o None si no hay nada que decir."""
    t = _limpiar(texto)
    if not t:
        return None
    mp3 = bytearray()
    try:
        comunicador = edge_tts.Communicate(t, TTS_VOZ, rate=TTS_VELOCIDAD)
    except ValueError:  # voz o velocidad mal escritas en .env: no quedarse mudo
        comunicador = edge_tts.Communicate(t, "es-MX-DaliaNeural")
    async for chunk in comunicador.stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ogg, err = await proc.communicate(bytes(mp3))
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {err.decode()[:200]}")
    return ogg
