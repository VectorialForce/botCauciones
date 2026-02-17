FROM python:3.12-slim

WORKDIR /app

# Dependencias de sistema: psycopg2, Chrome, Xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    wget \
    gnupg \
    xvfb \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y \
    google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Healthcheck - usa variables de entorno del container
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python db_check.py || exit 1

# Xvfb crea un display virtual para que Chrome corra con GUI
CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 &  export DISPLAY=:99 && python main.py"]