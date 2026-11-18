---
nombre: archivos-personales
cuando: recibos, nóminas, documentos o fotos que no son de la escuela; organizar carpetas de JARVIS; sacar datos o un Excel de ellos
---
Su espacio personal es JARVIS/archivos (una carpeta por tema: PLEXUS, Nominas Foxconn…). Organizarlo lo hace Claude.

1. Ve qué llegó con `archivos_entrada` y qué hay con `archivos_listar`.
2. `delegar_a_claude` con espacio "personal", `carpeta` del tema y en el prompt los nombres EXACTOS de los
   archivos de la entrada + lo que quiere (acomodar, renombrar, Excel…). Claude ya conoce las reglas de JARVIS
   (nóminas en <EMPRESA>/Semana N, PDF+XML juntos, nunca borrar).
3. Al terminar le llega el resumen; si quiere el archivo, `mandar_archivo`.
- Documentos muy sensibles (e.firma, contraseñas, datos bancarios): recomiéndale guardarlos en iCloud/gestor de
  contraseñas, fuera del servidor.
