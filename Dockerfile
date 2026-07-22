FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PK_HOST=0.0.0.0 \
    PK_PORT=8080 \
    PK_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY projektkontor ./projektkontor
RUN useradd --system --uid 10001 projektkontor && mkdir -p /data && chown -R projektkontor:projektkontor /data /app
USER projektkontor
EXPOSE 8080
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "120", "--graceful-timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "100", "--limit-request-fields", "50", "--limit-request-field_size", "8190", "--worker-tmp-dir", "/tmp", "--bind", "0.0.0.0:8080", "projektkontor.server:create_application()"]
