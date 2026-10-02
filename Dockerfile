FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

RUN useradd --system --no-create-home bot && chown -R bot /app
USER bot

# Konfigurace výhradně přes env (DISCORD_TOKEN, DATABASE_URL, ...), .env se do image nekopíruje.
# Migrace spouští bot při startu sám (AUTO_MIGRATE, výchozí zapnuto).
# Healthcheck: bot nevystavuje HTTP, ověřujeme jen, že se aplikace dá importovat.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import bot, config, db.config" || exit 1

CMD ["python", "main.py"]
