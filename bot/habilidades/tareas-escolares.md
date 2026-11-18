---
nombre: tareas-escolares
cuando: hacer una tarea, investigación, reporte, ejercicios o cualquier entregable de una materia
---
Una tarea buena sale de un encargo completo. Tu trabajo es juntar el encargo y mandárselo a Claude en UNA vuelta.

1. Junta lo que ya sabes, sin preguntar: materia (de lo que dijo o del contexto), parcial actual, si es de Classroom
   (`classroom_tareas`: instrucciones del profe, fecha, adjuntos ya bajados en su carpeta) y archivos que te mandó.
2. Lo que falte, pregúntalo en UN solo mensaje, máximo 3 cosas, solo si cambia el resultado:
   - formato (Word / PDF / PowerPoint / a mano) → botones
   - extensión o número de ejercicios
   - requisitos de la rúbrica o del profe
   Incluye el botón "Hazla con lo que hay". Nunca preguntes la materia si ya es obvia.
3. Abre la habilidad `encargar-a-claude` y manda `delegar_a_claude` (espacio escuela, materia, tipo tarea; parcial solo
   si es otro que el actual). Si es presentación o video para la materia, abre `presentaciones` o `videos`.
4. Avísale en una línea: qué va a entregar Claude y en qué formato.
Terminado = `delegar_a_claude` respondió "Tarea enviada".

- "A mano / al cuaderno": pide a Claude las respuestas en un Word claro para copiarlas.
- Corregir lo que entregó Claude → `delegar_a_claude` con continuar_sesion=true y solo qué cambiar.
- Al terminar le llega con su revisión: si marca ⚠️ (sin portada, nombre sin su registro) ofrécele corregirlo.
