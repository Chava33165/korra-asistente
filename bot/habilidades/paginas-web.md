---
nombre: paginas-web
cuando: su servidor de páginas web, su página, el hub, la entrevista de empleo en la web, publicar una página o portafolio
---
Su servidor de páginas (Caddy en la Raspberry, ~/Servicios/paginas-web):
- Público: https://paginas.tu-dominio.com (por Cloudflare). Con Tailscale también http://100.64.0.10:8090.
- Portada = hub sin login con un botón por página. Páginas:
  - **Empleo** (/empleo/, CON login: usuario chava): entrevista con IA que va armando su perfil, su perfil para
    corregirlo, vacantes y correos.
  - **Portafolio** (/portafolio/): todavía "pronto".
- Las páginas viven en ~/Servicios/paginas-web/sitios/<nombre>/ y se agregan al hub en sitios/hub/sitios.json.
  Para crear o cambiar una página nueva (por ejemplo su portafolio), que se lo pida a Claude Code desde su Mac:
  KORRA todavía no puede escribir ahí.
- Si olvidó la contraseña de Empleo: se cambia en el servidor (LEEME de paginas-web); no la tienes tú.
