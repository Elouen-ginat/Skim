from __future__ import annotations

from typing import Any

COMPOSE_SERVICES: dict[str, dict[str, Any]] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "ports": ["- '5432:5432'"],
        "environment": [
            "- POSTGRES_USER=skaal_user",
            "- POSTGRES_PASSWORD=skaal_pass",
            "- POSTGRES_DB=skaal_db",
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U skaal_user -d skaal_db"],
            "interval": "5s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "10s",
        },
    },
    "redis": {
        "image": "redis:7-alpine",
        "ports": ["- '6379:6379'"],
        "environment": [],
        "healthcheck": {
            "test": ["CMD", "redis-cli", "ping"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 5,
            "start_period": "10s",
        },
    },
    "traefik": {
        "image": "traefik:v3",
        "ports": ["- '80:80'"],
        "environment": [],
        "volumes": ["- /var/run/docker.sock:/var/run/docker.sock:ro"],
        "command": [
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            "--entrypoints.web.address=:80",
        ],
        "healthcheck": {
            "test": ["CMD", "traefik", "healthcheck"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "5s",
        },
    },
    "kong": {
        "image": "kong:3",
        "ports": ["- '8080:8000'"],
        "environment": [
            "- KONG_DATABASE=off",
            "- KONG_DECLARATIVE_CONFIG=/kong/config.yml",
            "- KONG_PROXY_LISTEN=0.0.0.0:8000",
            "- KONG_ADMIN_LISTEN=127.0.0.1:8001",
        ],
        "volumes": ["- ./kong.yml:/kong/config.yml:ro"],
        "healthcheck": {
            "test": ["CMD", "kong", "health"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "15s",
        },
    },
}


__all__ = ["COMPOSE_SERVICES"]
