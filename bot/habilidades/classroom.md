---
nombre: classroom
cuando: tareas, anuncios, materiales, calificaciones o profes de Classroom; "¿qué me falta entregar?", "¿cómo voy?"
---
Classroom es SOLO LECTURA (cuenta 'escuela'): él entrega desde la app; tú le preparas el archivo (con Claude) y le recuerdas.

- Pendientes: ya están en tu prompt; para el detalle `classroom_tareas` (filtro pendientes/entregadas/calificadas/todas).
  Estados: NEW/CREATED = sin entregar · TURNED_IN = entregada · RETURNED = calificada/devuelta.
- "¿Cómo voy?" → `classroom_tareas` filtro calificadas por materia: calificación/puntos y qué le falta.
- Anuncios del profe → `classroom_anuncios`; apuntes y presentaciones → `classroom_materiales`; correos → `classroom_profesores`.
- Ya la entregó → `tarea_marcar_hecha` (deja de insistir y su evento pasa a 🟢).
- Hacer una tarea de Classroom → abre la habilidad `tareas-escolares`.
- Lo automático (cada 15 min): tareas y anuncios nuevos llegan solos con botones, se agendan en 🟡 y bajan los
  adjuntos a SEMESTRE_N/MATERIA/Px/<tarea>/ con instrucciones.md. Una materia que ya no lleva → `classroom_ignorar`.
