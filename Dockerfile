FROM python:3.11-slim

WORKDIR /app

# System deps for Pillow / instagrapi
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# SQLite + Instagram session live in /app/data so we can mount a volume
RUN mkdir -p /app/data
ENV INSTAGROW_DB_PATH=/app/data/instagrow.db

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
