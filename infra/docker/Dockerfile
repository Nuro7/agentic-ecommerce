FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir hatchling && pip install --no-cache-dir .

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .
EXPOSE 8000
# Run DB migrations, then start the server. Uses sh -c so && and ${PORT} are
# interpreted by a shell (Render's command box mangles inline && / quotes).
CMD ["sh", "-c", "alembic upgrade head && uvicorn src.app.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
