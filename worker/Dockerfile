FROM node:22-bookworm-slim

# Herramientas que usan las skills docx/pdf/pptx/xlsx: LibreOffice (convertir a PDF,
# recalcular fórmulas), pandoc, poppler (pdftoppm), qpdf, fuentes.
# chromium + ffmpeg: las skills de video (Remotion, HyperFrames) renderizan con un navegador headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        libreoffice-writer libreoffice-impress libreoffice-calc \
        pandoc poppler-utils qpdf \
        fonts-dejavu fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
        git ca-certificates curl tini \
        chromium ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
        python-docx python-pptx openpyxl pypdf pdfplumber reportlab \
        "markitdown[docx,pptx,xlsx,pdf]" \
        fastapi "uvicorn[standard]"

# Claude Code + librerías JS que usan las skills para generar docx/pptx.
RUN npm install -g @anthropic-ai/claude-code docx pptxgenjs && npm cache clean --force
ENV NODE_PATH=/usr/local/lib/node_modules \
    DISABLE_AUTOUPDATER=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY worker.py revisar.py ./

# El usuario "node" de la imagen ya es uid/gid 1000 = mismo dueño que CETI COLOMOS en el host.
RUN mkdir -p /home/node/.claude /home/node/.agents /data && chown -R node:node /home/node /data
USER node

ENTRYPOINT ["tini", "--"]
CMD ["python3", "-m", "uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8000"]
