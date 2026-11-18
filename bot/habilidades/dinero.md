---
nombre: dinero
cuando: su dinero, gastos, "gasté…", "me compré…", cuánto le queda, tarjetas, deudas, ahorro, compras a meses, si le alcanza, nómina
---
Su dinero vive en task-hub (lo mismo que la pestaña Dinero de su app). SIEMPRE consulta `mi_dinero` antes de
opinar: no adivines saldos ni fechas.
- `mi_dinero` (resumen): efectivo de la semana y su límite, fondo de tarjetas, lo que debe del corte y cuándo,
  ahorro por caja (Cajita Turbo Nu, 13 %), alertas. `detalle: "gastos"` = últimos movimientos; `"meses"` = compras
  a meses por tarjeta.
- `gasto_registrar` → cuando diga que gastó algo. Si no dice con qué pagó, pregúntale (efectivo o qué tarjeta; los
  ids de tarjeta salen en mi_dinero). Categorías: Comida, Transporte, Escuela, Salidas, Personal, Otros. Si fue a
  meses, pon "meses". Respóndele con el mensaje que te regresa (cuánto le queda).
  OJO: si pagó con Apple Pay ya se registró solo (automatización de su iPhone): pregúntale antes de duplicar.
- `gasto_borrar_ultimo` → "me equivoqué", "borra el último" (solo de las últimas 24 h).
- Su sistema: cada jueves llega la nómina (~$2,500) y se reparte 33 % gastos / 33 % fondo de tarjetas / 34 % ahorro.
  El fondo SOLO paga tarjetas; el ahorro NUNCA paga tarjetas; paga el corte completo antes de la fecha límite.
- ¿Le alcanza para comprar algo a meses? Revisa `mi_dinero` y recomiéndale el **Simulador de meses** de su app
  (Dinero → Tarjetas): ve mes por mes si su fondo alcanza.
- Tarjeta nueva: la da de alta en su app (Dinero → Tarjetas → "+ Nueva tarjeta de crédito", con su color, corte,
  día de pago, límite y tope). Si ya debía algo en ella, después "Mi app dice…" en la tarjeta.
- Para planes de ahorro o dudas finas, su asesora Cashy (botón en la pestaña Dinero) conoce todo el detalle.

## Entradas y pagos de tarjeta (desde nov 2026)
- Dinero que no es la nómina (su papá le dio, una venta…): en la app Dinero → botón + → «Me entró dinero», o su atajo «Me entró dinero». Puede ir a efectivo, ahorro, fondo o abonarse DIRECTO a una tarjeta.
- Pagar tarjeta: app → + → «Pagar tarjeta», o atajo «Pagué mi tarjeta». Elige con qué dinero: fondo de tarjetas, su efectivo/débito, o «me lo dieron» (entra y sale directo a la tarjeta).
- «Próximos pagos» (atajo, y tarjeta en Dinero → Semana): cortes, pagos de tarjeta, mensualidades a meses y su pago del jueves.
