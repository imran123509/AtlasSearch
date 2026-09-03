# syntax=docker/dockerfile:1
# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime ----
FROM python:3.12-slim
RUN groupadd -r atlas && useradd -r -g atlas -u 10001 atlas
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=atlas:atlas src/ ./src/
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1

# tldextract reads a bundled public-suffix snapshot; make sure it never tries
# to refresh it over the network at runtime.
ENV TLDEXTRACT_CACHE=/tmp/tldextract

USER atlas
EXPOSE 9100

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import socket;socket.create_connection(('127.0.0.1',9100),2)"

ENTRYPOINT ["python", "-m", "atlas_crawler.main"]
CMD ["--metrics-port", "9100"]
