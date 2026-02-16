FROM python:3.12-slim

WORKDIR /app

# Dependencias de sistema para psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Healthcheck - usa variables de entorno del container
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python db_check.py || exit 1

CMD ["python", "main.py"]
