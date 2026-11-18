---
nombre: diseno
cuando: invitaciones, flyers, pósters, portadas, banners, felicitaciones, stickers o cualquier imagen para compartir
---
Los hace Claude como PNG listo para compartir (`delegar_a_claude` con espacio "personal" y tipo "diseno"; si es para
una materia, espacio "escuela" + materia). Quedan en Documentos/Creaciones/Diseños/<fecha título> y le llegan como
foto para verlo al instante y además como archivo original sin comprimir (el bueno para imprimir o subir).

1. Junta: qué es, ocasión, fecha ABSOLUTA, hora, lugar, nombres, textos exactos, para quién, estilo/tono/colores,
   formato (vertical/historia 1080x1920 · post cuadrado 1080x1080 · horizontal 1920x1080 · carta).
2. Si falta algo imprescindible (hora o lugar de una fiesta), pregúntalo en UN mensaje con un botón
   "Hazla así, lo relleno yo" (entonces Claude lo deja como "por confirmar"). Formato sin decir → vertical.
3. Con lo necesario (o cuando diga "hazla así", "dale", "va") → `delegar_a_claude` EN ESE MISMO mensaje, con todo
   lo del paso 1 en el prompt. Pide "2 opciones" si quiere variantes.
4. Avísale corto que Claude ya está en eso.
Terminado = `delegar_a_claude` respondió "Tarea enviada".

- Cambios ("más colores", "ponle la hora 8 pm") → continuar_sesion=true con solo el cambio.
