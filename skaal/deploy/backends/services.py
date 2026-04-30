from __future__ import annotations

from skaal.types import LocalServiceSpec

LOCAL_SERVICE_SPECS: dict[str, LocalServiceSpec] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "ports": [{"internal": 5432, "external": 5432}],
        "envs": [
            "POSTGRES_USER=skaal_user",
            "POSTGRES_PASSWORD=skaal_pass",
            "POSTGRES_DB=skaal_db",
        ],
        "healthcheck": {
            "tests": ["CMD-SHELL", "pg_isready -U skaal_user -d skaal_db"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "10s",
        },
    },
    "redis": {
        "image": "redis:7-alpine",
        "ports": [{"internal": 6379, "external": 6379}],
        "envs": [],
        "healthcheck": {
            "tests": ["CMD", "redis-cli", "ping"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 5,
            "startPeriod": "10s",
        },
    },
    "traefik": {
        "image": "traefik:v3",
        "ports": [{"internal": 80, "external": 80}],
        "envs": [],
        "volumes": [
            {
                "containerPath": "/var/run/docker.sock",
                "hostPath": "/var/run/docker.sock",
                "readOnly": True,
            }
        ],
        "command": [
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            "--entrypoints.web.address=:80",
        ],
        "healthcheck": {
            "tests": ["CMD", "traefik", "healthcheck"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "5s",
        },
    },
    "kong": {
        "image": "kong:3",
        "ports": [{"internal": 8000, "external": 8080}],
        "envs": [
            "KONG_DATABASE=off",
            "KONG_DECLARATIVE_CONFIG=/kong/config.yml",
            "KONG_PROXY_LISTEN=0.0.0.0:8000",
            "KONG_ADMIN_LISTEN=127.0.0.1:8001",
        ],
        "healthcheck": {
            "tests": ["CMD", "kong", "health"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "startPeriod": "15s",
        },
    },
}

LOCAL_FALLBACK: dict[tuple[str, str], str] = {
    ("dynamodb", "kv"): "local-map",
    ("firestore", "kv"): "local-map",
    ("gcs", "blob"): "local-blob",
    ("cloud-sql-postgres", "kv"): "local-redis",
    ("cloud-sql-postgres", "relational"): "sqlite",
    ("cloud-sql-pgvector", "vector"): "chroma-local",
    ("memorystore-redis", "kv"): "local-redis",
    ("rds-pgvector", "vector"): "chroma-local",
    ("s3", "blob"): "local-blob",
}
