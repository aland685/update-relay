# -*- coding: utf-8 -*-
"""应用配置加载模块。支持从环境变量与 .env 文件读取配置。"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    """轻量读取 .env 文件到 os.environ（不覆盖已设置的环境变量）。"""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    admin_username: str
    admin_password: str
    session_secret: str
    public_base_url: str
    github_token: str
    data_dir: Path
    cache_dir: Path
    db_path: Path
    metadata_ttl_seconds: int
    cache_ttl_seconds: int
    cache_ttl_mode: str
    cleanup_interval_seconds: int
    max_cache_mb: int
    upstream_timeout_seconds: int

    @property
    def max_cache_bytes(self) -> int:
        return self.max_cache_mb * 1024 * 1024

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        if env_file is not None:
            _load_dotenv(env_file)
        else:
            _load_dotenv(Path(".env"))

        data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        cache_dir = Path(os.environ.get("CACHE_DIR", str(data_dir / "cache"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        ttl_mode = os.environ.get("CACHE_TTL_MODE", "sliding").strip().lower()
        if ttl_mode not in ("sliding", "fixed"):
            ttl_mode = "sliding"

        secret = os.environ.get("SESSION_SECRET", "").strip()
        if not secret:
            secret = secrets.token_urlsafe(32)

        public_base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

        return cls(
            host=os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_env_int("PORT", 18080, minimum=1),
            admin_username=os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin",
            admin_password=os.environ.get("ADMIN_PASSWORD", "admin123456").strip() or "admin123456",
            session_secret=secret,
            public_base_url=public_base,
            github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
            data_dir=data_dir,
            cache_dir=cache_dir,
            db_path=data_dir / "relay.db",
            metadata_ttl_seconds=_env_int("METADATA_TTL_SECONDS", 300, minimum=5),
            cache_ttl_seconds=_env_int("CACHE_TTL_SECONDS", 3600, minimum=10),
            cache_ttl_mode=ttl_mode,
            cleanup_interval_seconds=_env_int("CLEANUP_INTERVAL_SECONDS", 300, minimum=10),
            max_cache_mb=_env_int("MAX_CACHE_MB", 2048, minimum=10),
            upstream_timeout_seconds=_env_int("UPSTREAM_TIMEOUT_SECONDS", 300, minimum=10),
        )

    def with_overrides(self, overrides: dict[str, str]) -> "Settings":
        """应用持久化在数据库中的运行时配置覆盖项。"""
        if not overrides:
            return self

        def _int_val(key: str, current: int, minimum: int) -> int:
            raw = overrides.get(key)
            if raw is None or raw == "":
                return current
            try:
                return max(minimum, int(raw))
            except ValueError:
                return current

        ttl_mode = str(overrides.get("cache_ttl_mode", self.cache_ttl_mode)).strip().lower()
        if ttl_mode not in ("sliding", "fixed"):
            ttl_mode = self.cache_ttl_mode

        return replace(
            self,
            metadata_ttl_seconds=_int_val("metadata_ttl_seconds", self.metadata_ttl_seconds, 5),
            cache_ttl_seconds=_int_val("cache_ttl_seconds", self.cache_ttl_seconds, 10),
            cache_ttl_mode=ttl_mode,
            cleanup_interval_seconds=_int_val("cleanup_interval_seconds", self.cleanup_interval_seconds, 10),
            max_cache_mb=_int_val("max_cache_mb", self.max_cache_mb, 10),
        )
