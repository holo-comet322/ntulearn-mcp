"""Settings, read from environment variables (all optional)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass
class Settings:
    base_url: str
    data_dir: Path
    download_dir: Path

    @property
    def cookie_file(self) -> Path:
        return self.data_dir / "cookies.json"

    @property
    def profile_dir(self) -> Path:
        # Dedicated browser profile for the login window, so the SSO
        # "stay signed in" state survives between logins.
        return self.data_dir / "browser-profile"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def load_settings() -> Settings:
    return Settings(
        base_url=os.environ.get("NTULEARN_BASE_URL", "https://ntulearn.ntu.edu.sg").rstrip("/"),
        data_dir=_env_path("NTULEARN_DATA_DIR", Path.home() / ".ntulearn-mcp"),
        download_dir=_env_path("NTULEARN_DOWNLOAD_DIR", Path.home() / "NTULearn"),
    )


settings = load_settings()
