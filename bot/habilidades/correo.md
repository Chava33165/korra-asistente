---
nombre: correo
cuando: leer, buscar, resumir, organizar o mandar correos (Gmail escuela, personal y empleo); escribirle a un profe
---
Cuentas: 'escuela' (correo del CETI, para profes y trámites) y 'personal' (su Gmail, tu-correo@gmail.com).
Ese mismo Gmail es el que usa para buscar trabajo: 'empleo' es otro nombre de 'personal' (no son dos cuentas); ahí
llegan las respuestas de reclutadores a sus CVs.

## Leer y resumir
- `gmail_buscar` con sintaxis de Gmail: from:, subject:, has:attachment, is:unread, newer_than:7d, after:AAAA/MM/DD,
  category:primary. Gmail distingue acentos: si da 0, prueba sin/con acento o busca por remitente.
- `gmail_leer` para el cuerpo y adjuntos; `gmail_hilo` para la conversación completa.
- Resumen: primero profes/escuela/trabajo, luego lo demás; promociones solo como conteo. Una línea por correo.

## Organizar
`gmail_marcar`: leido, no_leido, archivar, destacar, importante, etiqueta:<nombre> (la crea), papelera (él confirma).

## Mandar
1. Destinatario: si es un profe → `classroom_profesores` para el correo exacto; nunca lo adivines.
2. Adjunto: ubica la ruta real con `listar_archivos` / `archivos_listar`.
3. Redacta: con profes formal y breve (saludo, motivo en 1-2 líneas, nombre completo, registro 26310453, grupo);
   con amigos, como él habla.
4. `gmail_enviar` (o `gmail_borrador` si solo quiere dejarlo listo). Le salen [📤 Enviar] [❌ Cancelar]:
   dile "revísalo y dale Enviar". Terminado = el correo está en pantalla esperando su botón.
- Guardar adjuntos de muchos correos (recibos, constancias) → `gmail_guardar_adjuntos` a JARVIS/archivos/<carpeta>.
