# syntax=docker/dockerfile:1
#
# BUILD CONTEXT MUST BE THE REPO ROOT, not indexer/:
#     docker build -f indexer/Dockerfile -t atlas/indexer .
#
# This service shares URL canonicalisation with the crawler (divergent
# canonicalisation would give one URL two fingerprints), so the crawler source
# has to be inside the context. A COPY cannot reach above its context root.

FROM python:3.12-slim AS builder
WORKDIR /app
COPY indexer/pyproject.toml ./
COPY crawler /crawler
RUN pip install --no-cache-dir --prefix=/install \
      selectolax charset-normalizer py3langid structlog prometheus-client \
      aiokafka aioboto3 tldextract

FROM python:3.12-slim
RUN groupadd -r atlas && useradd -r -g atlas -u 10001 atlas
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=atlas:atlas indexer/src/ ./src/
# urlnorm only; the crawler's __init__ is lazy so this pulls no fetch-stack deps.
COPY --chown=atlas:atlas crawler/src/atlas_crawler/ ./src/atlas_crawler/

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 TLDEXTRACT_CACHE=/tmp/tldextract
USER atlas
EXPOSE 9101

ENTRYPOINT ["python", "-m", "atlas_indexer.main"]
CMD ["--metrics-port", "9101"]
