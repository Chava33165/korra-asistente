---
nombre: presentaciones
cuando: pide una presentación, diapositivas, PowerPoint, una expo o slides (de la escuela o personal)
---
Las hace Claude en PowerPoint (skill pptx) y, si quiere, también en PDF. Van con plan y botón.

1. Junta (propón defaults):
   - Tema y para qué materia (si es de la escuela: espacio escuela + materia; va a su carpeta y parcial).
   - Número de diapositivas (default 8-12) y si lleva portada institucional (escuela: sí, con la de siempre).
   - Estilo y colores; imágenes (de lo que mandó, o ilustraciones simples; nada con marca de agua).
   - Notas del orador (útil para exponer) y si la quiere también en PDF.
   - Instrucciones del profe o de Classroom si las hay (`classroom_tareas`).
2. `delegar_a_claude` con tipo "presentacion" y `plan` de 3-4 líneas: número de diapositivas, estructura, estilo,
   PPTX/PDF. El bot le enseña el plan con [Hazlo] [Cambiar].
3. En el prompt pide: nombre `26310453_<Tema>.pptx` si es de la escuela, revisar que el texto no se salga de las
   cajas (convertir a PDF y mirar las páginas), y bibliografía al final si es investigación.

- Correcciones → continuar_sesion=true con solo lo que cambia.
