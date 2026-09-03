# Docker

Local development topology and image conventions.

Related: [KUBERNETES](KUBERNETES.md) · [OPENSEARCH](OPENSEARCH.md) · [MONITORING](MONITORING.md)

---

## Images

One image per service, all from a shared base.

| Image | Base | Contents |
| --- | --- | --- |
| `atlas/crawler` | `python:3.12-slim` | Fetcher, frontier, robots |
| `atlas/renderer` | `mcr.microsoft.com/playwright/python` | Headless Chromium — large, kept separate |
| `atlas/indexer` | `python:3.12-slim` | Parse, dedup, score, bulk load |
| `atlas/search-api` | `python:3.12-slim` | FastAPI |
| `atlas/frontend` | `nginx:alpine` | Static build output |

The renderer is a **separate image** because the Playwright base is ~1.5 GB. Bundling it into
the crawler image would make every crawler pull carry a browser it does not use.

---

## Dockerfile pattern

```dockerfile
# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev > requirements.txt \
 && uv pip install --system --no-cache -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim
RUN groupadd -r atlas && useradd -r -g atlas -u 10001 atlas
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --chown=atlas:atlas src/ ./src/

USER atlas
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/v1/healthz')"

ENTRYPOINT ["python", "-m", "src.main"]
```

Rules that matter:

- **Multi-stage.** Build tools do not ship to production.
- **Non-root, fixed UID.** UID must be explicit so Kubernetes `runAsUser` matches.
- **Lockfile copied before source.** Dependency layer caches across source edits — the single
  biggest build-time win.
- **`HEALTHCHECK` `start-period`** long enough for the process to warm; too short and the
  container is killed during startup.

---

## Compose topology

```yaml
name: atlas

services:
  opensearch:
    image: opensearchproject/opensearch:2.13.0
    environment:
      - discovery.type=single-node
      - OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g
      - DISABLE_SECURITY_PLUGIN=true        # dev only — never in a shared environment
    ulimits: { memlock: { soft: -1, hard: -1 }, nofile: { soft: 65536, hard: 65536 } }
    volumes: [ os-data:/usr/share/opensearch/data ]
    healthcheck:
      test: ["CMD-SHELL", "curl -fs localhost:9200/_cluster/health | grep -qE 'green|yellow'"]
      interval: 10s
      retries: 20

  redis:
    image: redis:7-alpine
    command: >
      redis-server --appendonly yes --maxmemory 1gb --maxmemory-policy allkeys-lru
    volumes: [ redis-data:/data ]
    healthcheck: { test: ["CMD","redis-cli","ping"], interval: 5s, retries: 10 }

  redpanda:                                  # Kafka-compatible, one binary, no ZooKeeper
    image: redpandadata/redpanda:latest
    command: >
      redpanda start --overprovisioned --smp 1 --memory 1G
      --kafka-addr PLAINTEXT://0.0.0.0:9092
      --advertise-kafka-addr PLAINTEXT://redpanda:9092
    healthcheck: { test: ["CMD","rpk","cluster","health"], interval: 10s, retries: 15 }

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment: [ MINIO_ROOT_USER=atlas, MINIO_ROOT_PASSWORD=atlas-dev-only ]
    volumes: [ minio-data:/data ]

  crawler:
    build: { context: ., dockerfile: crawler/Dockerfile }
    environment:
      - KAFKA_BROKERS=redpanda:9092
      - REDIS_URL=redis://redis:6379/0
      - S3_ENDPOINT=http://minio:9000
      - CRAWL_RATE_LIMIT=2          # dev: be conservative, you are crawling the real web
      - USER_AGENT=AtlasSearchBot/0.1 (+https://example.org/bot)
    depends_on:
      redpanda: { condition: service_healthy }
      redis:    { condition: service_healthy }

  indexer:
    build: { context: ., dockerfile: indexer/Dockerfile }
    environment: [ OPENSEARCH_URL=http://opensearch:9200, KAFKA_BROKERS=redpanda:9092 ]
    depends_on:
      opensearch: { condition: service_healthy }

  search-api:
    build: { context: ., dockerfile: search-api/Dockerfile }
    ports: [ "8000:8000" ]
    environment: [ OPENSEARCH_URL=http://opensearch:9200, REDIS_URL=redis://redis:6379/1 ]
    depends_on:
      opensearch: { condition: service_healthy }

  frontend:
    build: { context: ./frontend }
    ports: [ "3000:80" ]

volumes: { os-data: , redis-data: , minio-data: }
```

### Notes

- **Redpanda instead of Kafka** for local dev: one binary, no ZooKeeper/KRaft setup, same
  protocol. Production uses real Kafka ([KAFKA](KAFKA.md)).
- **`condition: service_healthy`**, not bare `depends_on`. OpenSearch takes ~40 s to become
  ready; without the condition the indexer crash-loops on startup.
- **Two Redis databases** (`/0` crawler, `/1` API). In production these are separate
  instances with different eviction policies ([REDIS](REDIS.md)).
- **`CRAWL_RATE_LIMIT=2`** in dev. You are crawling real websites from a laptop. Be
  conservative and make sure the `USER_AGENT` has a working contact URL.

---

## Resource reality

The full stack needs ~8 GB RAM. OpenSearch alone wants 4 GB (2 GB heap + page cache).

```bash
docker compose up -d opensearch redis redpanda   # infra only
docker compose up crawler indexer                # pipeline
docker compose up search-api frontend            # serving
```

For a laptop, run infra in Compose and the Python services natively — faster iteration, and
the volume-mount file-watching problem disappears.

---

## Anti-patterns

| Don't | Why |
| --- | --- |
| `latest` tags | Non-reproducible builds; a rebuild silently changes behaviour |
| Root user | Container escape becomes host root |
| Secrets in `ENV` | Visible in `docker inspect` and image history |
| One image for all services | Every pull carries every dependency |
| `depends_on` without `condition` | Race on startup, crash loops |
| Source copied before dependencies | Cache miss on every edit |
| `DISABLE_SECURITY_PLUGIN` outside dev | Unauthenticated cluster access |
| Building the frontend inside the API image | Node toolchain in a production Python image |
