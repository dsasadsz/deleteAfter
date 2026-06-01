FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
ENV LOG_FORMAT=json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin appuser

COPY . .
RUN mkdir -p /app/data /app/tmp/smoke /app/tmp/tts_cache \
    && chown -R appuser:appuser /app

EXPOSE 8000

USER appuser

# Keep workers=1 while WebSocket/session state is in-process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
