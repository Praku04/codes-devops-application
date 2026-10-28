FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOME=/home/appuser

WORKDIR /app

# System dependencies + LibreOffice (headless DOCX/PPTX/PPT/DOC → PDF on Linux/Azure Docker)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    ffmpeg \
    libsm6 \
    libxext6 \
    fontconfig \
    libreoffice \
    libreoffice-writer \
    libreoffice-impress \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f \
    && (libreoffice --version || soffice --version)

# Non-root runtime user (Sonar docker:S5674)
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

# Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# No tokenizer models are downloaded at build time: utils/text_tokenize.py replaced NLTK,
# so the build no longer needs network access beyond the package index.

# App code — explicit paths only (Sonar docker:S6520); keep secrets/.git out of the image
COPY main.py .
COPY core/ core/
COPY features/ features/
COPY models/ models/
COPY schemas/ schemas/
COPY services/ services/
COPY utils/ utils/
COPY static/ static/
COPY templates/ templates/

RUN mkdir -p /app/temp \
    && chown -R appuser:appuser /app /home/appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "3"]
