FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 app

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install . \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R app:app /app

USER app
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
