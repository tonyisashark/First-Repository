FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kalshi_bot ./kalshi_bot
RUN pip install --no-cache-dir .

# state db lives here; mount a volume to persist it across restarts
RUN mkdir -p /app/state
VOLUME ["/app/state"]

# default: paper trading on the demo exchange (safe). Override env to go live.
ENV KALSHI_ENV=demo DRY_RUN=true STATE_DB_PATH=/app/state/kalshi_bot.sqlite3

ENTRYPOINT ["kalshi-bot"]
CMD ["run"]
