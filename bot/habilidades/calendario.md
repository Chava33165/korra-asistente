---
nombre: calendario
cuando: crear, mover o borrar eventos; "¿qué tengo?", "¿estoy libre?", "¿hay clase?"; horario de clases; colores del calendario
---
Ves TODOS sus calendarios (cuentas 'escuela' y 'personal'). Tu agenda de ayer a +7 días ya está en tu prompt: contesta
"¿qué tengo?" con ella; para otras fechas usa `calendario_ver`.

## Crear
1. Saca la fecha ABSOLUTA de la tabla de fechas ("el lunes" = el próximo lunes). Hora sin duración → 1 h.
2. Falta la HORA → pregúntala (con botones si hay 2-3 opciones obvias). Falta el lugar → créalo sin lugar.
3. Antes de crear, revisa choques en tu agenda (o `calendario_libre`). Si choca, díselo y ofrece opciones con botones.
4. `calendario_crear`. Rutina ("gym lunes, miércoles y viernes 7am") = UN evento con repetir="semanal:lu,mi,vi".
   Cuenta: 'personal' por default; juntas de equipo/asesorías → 'escuela'. Puede llevar invitados (correo) y Meet.
5. Confírmale en una línea con lo que devolvió la herramienta: "📅 Dentista · lun 20 oct 10:00".
Terminado = la herramienta respondió "Evento creado" y se lo dijiste con fecha y hora.

## Mover / cambiar / borrar
- Busca el evento en tu agenda (trae id, calendario_id y, si se repite, serie_id). Si hay 2+ candidatos, pregunta cuál con botones.
- `calendario_editar` (con serie_id cambia toda la serie). `calendario_borrar` le saca botones de confirmar: dile "confírmalo abajo".

## Calendarios de colores (cuenta escuela; en el iPhone se ve el color del CALENDARIO)
🏫 Clases (azul pastel) · 🌴 CETI · Sin clases (suspensiones, vacaciones) · 📝 CETI · Evaluaciones (parciales,
recuperación, regularización) · 🎓 CETI · Fechas escolares (inicio/fin de semestre, inscripciones, pagos) ·
semáforo de tareas 🟡 por entregar · 🟢 entregadas · 🔴 vencidas. Con estos contestas "¿cuándo son los parciales?",
"¿hay clase el lunes?", "¿cuándo salgo de vacaciones?".
- Horario de clases: solo con `horario_cargar` y el horario COMPLETO (reemplaza el anterior; él confirma con botón).
- Tareas de Classroom: se agendan y cambian de color solas; tú solo usas `tarea_marcar_hecha`.
- Colores de evento sueltos: verde, verde claro, rojo, amarillo, naranja, morado, azul, azul claro, rosa, gris, lila.
