# Use official python slim image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser . .

USER appuser

# run with gunicorn + uvicorn workers
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--log-level", "info"]
