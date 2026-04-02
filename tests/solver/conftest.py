"""Solver test fixtures."""
import pytest


@pytest.fixture
def aws_catalog(tmp_path):
    """Write a minimal catalog TOML and return its path."""
    import tomli_w  # type: ignore[import]

    data = {
        "storage": {
            "redis": {
                "display_name": "Redis",
                "read_latency": {"min": 0.1, "max": 2.0, "unit": "ms"},
                "write_latency": {"min": 0.1, "max": 5.0, "unit": "ms"},
                "durability": ["ephemeral", "persistent"],
                "access_patterns": ["random-read", "random-write"],
                "cost_per_gb_month": 3.5,
            },
            "postgres": {
                "display_name": "Postgres",
                "read_latency": {"min": 1.0, "max": 50.0, "unit": "ms"},
                "write_latency": {"min": 2.0, "max": 100.0, "unit": "ms"},
                "durability": ["persistent", "durable"],
                "access_patterns": ["random-read", "transactional"],
                "cost_per_gb_month": 0.12,
            },
        }
    }
    catalog_path = tmp_path / "aws.toml"
    catalog_path.write_bytes(__import__("tomllib", fromlist=["dumps"]) and _write_toml(data))
    return catalog_path


def _write_toml(data: dict) -> bytes:
    """Minimal TOML serialiser for the fixture (avoids tomli_w dependency)."""
    import json, re

    def _val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return f'"{v}"'
        if isinstance(v, list):
            return "[" + ", ".join(_val(i) for i in v) + "]"
        return f'"{v}"'

    lines = []

    def _section(prefix, d):
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                lines.append(f"\n[{full}]")
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        lines.append(f"[{full}.{sk}]")
                        for ssk, ssv in sv.items():
                            lines.append(f"{ssk} = {_val(ssv)}")
                    else:
                        lines.append(f"{sk} = {_val(sv)}")

    _section("", data)
    return "\n".join(lines).encode()
