FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

RUN groupadd --system pprbot \
    && useradd --system --gid pprbot --create-home --home-dir /app pprbot \
    && mkdir -p /app/logs \
    && chown -R pprbot:pprbot /app

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=pprbot:pprbot backend /app/backend
COPY --chown=pprbot:pprbot alembic /app/alembic
COPY --chown=pprbot:pprbot alembic.ini /app/alembic.ini

USER pprbot

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
