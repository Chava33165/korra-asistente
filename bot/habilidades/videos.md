---
nombre: videos
cuando: pide un video, clip, reel, TikTok, animación, video de cumpleaños o con fotos, o un video para una materia
---
Los hace Claude con Remotion o HyperFrames (skills instaladas) y los renderiza en la Pi. Gastan bastante límite
(6-14 min, $0.5-1.6) y tardan: por eso van con plan y botón.

1. Junta (lo que no diga, propón tú un default razonable):
   - Para qué y para quién; textos exactos, nombres, fechas.
   - Duración (default 15-30 s; máx. ~2 min) y formato: vertical 1080x1920 (historia/TikTok, default) u horizontal 1920x1080.
   - Fotos o videos que mandó: revisa `archivos_entrada` y pon en el prompt los nombres EXACTOS de TODOS.
   - Voz (narración en español mexicano con edge-tts) o solo texto; música (Claude no baja música con derechos:
     sin música o con un fondo libre).
   - Estilo: divertido, elegante, tipo tarjeta, cinemático…
2. `delegar_a_claude` con tipo "video" (espacio personal, o escuela + materia si es para una clase) y `plan` de 3-4
   líneas: qué se ve, duración, formato, voz/música. El bot le enseña el plan con [Hazlo] [Cambiar].
3. En el prompt pide: proyecto en `_proyecto/`, render final en la carpeta, revisar duración y resolución con ffprobe
   y mirar fotogramas, y borrar `node_modules` al terminar.
4. Si pesa más de 50 MB le llega una copia comprimida; el original queda en Samba.

- Cambios chicos (un texto, un color) → continuar_sesion=true. Cambios grandes → mejor un encargo nuevo (seguir
  la sesión de un video cuesta $2-5).
