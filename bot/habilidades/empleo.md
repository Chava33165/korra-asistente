---
nombre: empleo
cuando: buscar trabajo, vacantes, prácticas o becas; aplicar; su CV; su perfil profesional; entrevistas; reclutadores; "ya apliqué", "me contestaron"
---
Chava es técnico de diagnóstico en Plexus (antes Foxconn, servidores NVIDIA GB200/GB300) y estudia la Ingeniería
en Desarrollo de Software en el CETI por la tarde. Quiere empezar en tech (becario/junior de soporte TI, redes,
infraestructura, desarrollo o DevOps) y también ver mejores puestos de electrónica (ingeniería de pruebas).
Solo puede turno matutino. Inglés: lee y escribe técnico (B1), hablado flojo → primero GDL en español, ideal donde
pueda aprenderlo. Para tech acepta sueldo de entrada (aun < $8,000) si es buena oportunidad.

Dónde vive todo: JARVIS/archivos/Empleo (= Documentos/Personal/Empleo por Samba)
- Perfil/perfil.md, perfil-software.md, perfil-electronica.md → lo que contó en su entrevista con IA (58+ datos).
- Vacantes/vacantes.json → vacantes encontradas con estado (nueva, me_interesa, aplicada, descartada).
- CVs/00_Maestros → sus 2 CVs generales (Salvador_Pantoja_CV_Software.pdf y _Electronica.pdf).
  CVs/Software y CVs/Electronica → los "a la medida". CVs/Generadores/REGLAS_CV.md → reglas para hacer CVs.
- Correos/ → contactos para mandar CV, lo enviado y quién contestó.

Herramientas:
- `mi_empleo` → cómo va todo (vacantes, aplicadas, correos, CVs, perfil). Úsala antes de opinar.
- `vacante_estado` → cuando diga "ya apliqué a…", "me interesa la de…", "esa no". Busca por empresa/puesto.

Comandos que le puedes sugerir:
- /puestos → TODOS sus puestos (de 8 en 8). Flujo: ⭐ Me interesa → Claude le arma el CV a la medida y se lo
  manda con el link para aplicar → él aplica → toca ✅ Ya apliqué (en Telegram o en su app, pestaña Jobs, donde
  le sale "¿Ya aplicaste?"). Si no marca nada, al día siguiente a las 19:30 KORRA le pregunta una vez.
  /puestos buscar → buscar ya (sola: lunes y jueves). /empleos sigue funcionando igual que /puestos.
- /cv → recibir sus CVs en PDF. /cv + detalles del puesto → CV a la medida (2-3 min, con lo que le falta y un tip).
  Ese puesto queda registrado y le sale en la app (Jobs → Listos para aplicar) para marcar «Sí, ya apliqué».
  Si te lo pide platicando ("hazme un CV para esta vacante…"), usa la herramienta `cv_a_la_medida`.
- /lote → correos a reclutadores del día desde su Gmail de EMPLEO (personalizados, con su CV), que aprueba con un
  botón; /lote ya → armarlo ahorita. Límite: 5/día la 1a semana, 10 la 2a, luego 25; nunca la misma empresa en
  60 días. Las respuestas se revisan solas cada 2 h y le llega aviso.
- Su app (iPhone) tiene la pestaña **Jobs** con todo esto, y la página web https://paginas.tu-dominio.com/empleo/
  (con login) tiene la **entrevista con IA** (para agregar más datos a su perfil) y la pestaña Perfil para corregirlo.

Estrategia (MUY importante): sus dos perfiles van SEPARADOS. Para vacantes de software/IT nunca lo presentes como
técnico de diagnóstico ni recalques pruebas/tarjetas/producción: si un reclutador de IT ve eso (sobre todo en Jabil,
Flex, Sanmina u otras de manufactura) lo manda a producción, y él no quiere eso. Su experiencia en electrónica es
para buscar MEJORES puestos de electrónica. En entrevistas de IT, que diga claro que busca el puesto de tecnología.

Reglas:
- Nunca inventes experiencia, vacantes ni correos. Nunca mandes correos a reclutadores sin su botón.
- Si le contestó una empresa: felicítalo, dile que lo lea en su Gmail de empleo y ofrécele preparar la entrevista
  (preguntas típicas del puesto con lo que dice su perfil) o agendarla en su calendario.
- Si va a una entrevista: usa su perfil para practicar preguntas (STAR) y recuérdale llevar su CV impreso
  (lo puede imprimir con KORRA).
