from __future__ import annotations

import os
from pathlib import Path


def pulumi_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    return env


def local_backend_url(state_dir: Path) -> str:
    posix_path = state_dir.resolve().as_posix()
    return f"file://{posix_path}"


def pulumi_login_local(state_dir: Path) -> str:
    resolved_state_dir = state_dir.resolve()
    resolved_state_dir.mkdir(parents=True, exist_ok=True)
    return local_backend_url(resolved_state_dir)
