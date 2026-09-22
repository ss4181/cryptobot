# =============================================================================
#  cryptobot -- paper trading (SIMULATION ONLY)
#
#  Runs the same verification/backtest/paper loop as a local checkout.
#  No exchange API keys are used or required: only Binance's public REST data.
#
#  OPTIONAL mobile notifications need no extra dependency (stdlib HTTP only).
#  Pass the secrets at run time, never bake them into the image, e.g.:
#    docker run --rm \
#      -e CRYPTOBOT_NTFY_TOPIC=cryptobot-paper-<random> \
#      -e CRYPTOBOT_TELEGRAM_BOT_TOKEN=... -e CRYPTOBOT_TELEGRAM_CHAT_ID=... \
#      -e CRYPTOBOT_WEBHOOK_URL=... \
#      -v "$PWD/cryptobot/logs:/app/cryptobot/logs" \
#      cryptobot-paper python -m cryptobot notify test
#  Guide (Turkish): cryptobot/NOTIFICATIONS.md
# =============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    TZ=UTC

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY cryptobot/requirements.txt /app/cryptobot/requirements.txt
RUN pip install --no-cache-dir -r /app/cryptobot/requirements.txt

# Application code (caches/logs/reports/state are created at runtime).
COPY cryptobot/ /app/cryptobot/

# Run as a non-root user; the bot only writes inside /app/cryptobot.
RUN useradd --create-home --uid 10001 cryptobot \
    && mkdir -p /app/cryptobot/data/cache /app/cryptobot/logs /app/cryptobot/reports /app/cryptobot/run \
    && chown -R cryptobot:cryptobot /app
USER cryptobot

# `python -m cryptobot ...` works here (normal CPython, no ._pth quirk).
ENTRYPOINT ["python", "-m", "cryptobot"]
CMD ["safety"]
