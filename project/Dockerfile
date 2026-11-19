# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

ENV FLASK_ENV=production \
    DATABASE_PATH=/data/customer_support.db \
    PORT=8000
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
