---
nombre: encargar-a-claude
cuando: antes de cada delegar_a_claude (escribir un buen encargo, elegir tipo y carpeta, seguir o corregir un trabajo de Claude, rutas)
---
Claude no ve tu plática ni tu personalidad, y cada encargo gasta su límite: el encargo debe ser autocontenido y único.

Escribe el `prompt` en español neutro, segunda persona, con:
1. Objetivo en una frase.
2. Entregable exacto: formato, extensión, estructura, nombre de archivo si importa.
3. Contexto y datos: fechas absolutas, textos exactos, instrucciones del profe, rutas EXACTAS de los archivos que mandó
   (los nombres de la entrada de Telegram, todos, no solo el último).
4. Calidad: qué debe verificar antes de terminar (ortografía, cálculos, que abra el archivo, duración del video).
5. Lo que no sabes: "si falta X, usa un supuesto razonable y dilo en tu resumen".

## Tipo y carpeta (los decide `tipo` + `espacio` + `materia`; tú no escribes rutas)
| Pedido | espacio | tipo | Queda en |
|---|---|---|---|
| Tarea, ensayo, ejercicios de una materia | escuela + materia | tarea | Documentos/Escuela/…/<MATERIA>/<parcial actual> |
| Presentación o video PARA una materia | escuela + materia | presentacion / video | igual, en su materia |
| Video personal (cumple, TikTok, recuerdo) | personal | video | Documentos/Creaciones/Videos/<fecha título> |
| Flyer, invitación, póster, sticker | personal | diseno | Documentos/Creaciones/Diseños/<fecha título> |
| Presentación que no es de la escuela | personal | presentacion | Documentos/Creaciones/Presentaciones/<fecha título> |
| Acomodar nóminas, recibos, papeles | personal + carpeta | archivos | Documentos/Personal/<carpeta> |
Sin parcial → el parcial actual. Video y presentación llevan `plan` (3-4 líneas): Chava lo aprueba con un botón antes
de que Claude arranque, así que NO le preguntes "¿lo hago?" aparte.

## Después
- Al terminar, el bot le manda solo y con su revisión (✅/⚠️, páginas, diapositivas, duración) los archivos finales;
  tú no los mandes otra vez con `mandar_archivo` salvo que él lo pida.
- Si la revisión marca ⚠️ (sin portada, sin su registro, video vacío) ofrécele corregirlo.
- Corregir o terminar el ÚLTIMO trabajo → continuar_sesion=true; el prompt dice solo qué cambia. Ojo: seguir la
  sesión de un video es caro ($2-5); si el cambio es grande, mejor un encargo nuevo con los cambios.
- Rutas para Chava: como en Samba (Documentos/Escuela/…, Documentos/Creaciones/…, Documentos/Personal/…).
- Claude apagado (/claude off) → díselo y ofrece lo que tú sí puedes hacer.
- Si él quiere trabajar ÉL MISMO con Claude en una carpeta → habilidad `sesion-pc`, no `delegar_a_claude`.
