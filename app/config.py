"""Configuration loading and validation.

Two layers of configuration:

1. ``config.yaml`` — seed data (initial sources/keywords/poll interval) read only
   on first boot, plus operational tunables. Validated for structure at startup.
2. Environment variables — required secrets (admin creds, DB connection, secret
   key) and optional overrides of the operational tunables.

We fail fast with a clear message if anything required is missing or malformed,
rather than half-booting.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


@dataclass
class SourceSeed:
    name: str
    url: str
    enabled: bool = True


@dataclass
class KeywordSeed:
    pattern: str
    enabled: bool = True


@dataclass
class AppConfig:
    """Fully-resolved, validated application configuration."""

    # Secrets / connection (from env)
    admin_user: str
    admin_password_hash: str
    secret_key: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # Operational tunables (config.yaml, env-overridable)
    poll_interval_minutes: int
    body_truncate_bytes: int
    regex_timeout_seconds: float
    output_item_cap: int
    retention_max_matches: int
    retention_max_age_days: int
    http_connect_timeout_seconds: float
    http_read_timeout_seconds: float
    http_max_retries: int
    poll_max_workers: int

    # First-boot seed data (config.yaml)
    source_seeds: list[SourceSeed] = field(default_factory=list)
    keyword_seeds: list[KeywordSeed] = field(default_factory=list)

    @property
    def sqlalchemy_url(self) -> str:
        # mysqlclient driver via SQLAlchemy's "mysqldb" dialect.
        return (
            f"mysql+mysqldb://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Required environment variable '{name}' is missing or empty.")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable '{name}' must be an integer, got {raw!r}.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable '{name}' must be a number, got {raw!r}.") from exc


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _yaml_int(data: dict, key: str, default: int) -> int:
    val = data.get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config.yaml: '{key}' must be an integer, got {val!r}.") from exc


def _yaml_float(data: dict, key: str, default: float) -> float:
    val = data.get(key, default)
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config.yaml: '{key}' must be a number, got {val!r}.") from exc


def load_config(config_path: str | None = None) -> AppConfig:
    """Load and validate configuration. Raises :class:`ConfigError` on problems."""
    config_path = config_path or os.environ.get("CONFIG_PATH", "config.yaml")

    if not os.path.exists(config_path):
        raise ConfigError(f"config.yaml not found at '{config_path}'.")

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a mapping at the top level.")

    # --- Required secrets / connection from environment ---
    admin_user = _require_env("ADMIN_USER")
    admin_password_hash = _require_env("ADMIN_PASSWORD_HASH")
    if not admin_password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        raise ConfigError(
            "ADMIN_PASSWORD_HASH does not look like a bcrypt hash. "
            "Generate one with `python gen_password_hash.py`."
        )
    secret_key = _require_env("SECRET_KEY")
    db_host = _require_env("DB_HOST")
    db_name = _require_env("DB_NAME")
    db_user = _require_env("DB_USER")
    db_password = _require_env("DB_PASSWORD")
    db_port = _env_int("DB_PORT", 3306)

    # --- Operational tunables: config.yaml value, env override wins ---
    poll_interval_minutes = _env_int(
        "POLL_INTERVAL_MINUTES", _yaml_int(raw, "poll_interval_minutes", 15)
    )
    body_truncate_bytes = _env_int(
        "BODY_TRUNCATE_BYTES", _yaml_int(raw, "body_truncate_bytes", 102400)
    )
    regex_timeout_seconds = _env_float(
        "REGEX_TIMEOUT_SECONDS", _yaml_float(raw, "regex_timeout_seconds", 2.0)
    )
    output_item_cap = _env_int("OUTPUT_ITEM_CAP", _yaml_int(raw, "output_item_cap", 200))
    retention_max_matches = _env_int(
        "RETENTION_MAX_MATCHES", _yaml_int(raw, "retention_max_matches", 5000)
    )
    retention_max_age_days = _env_int(
        "RETENTION_MAX_AGE_DAYS", _yaml_int(raw, "retention_max_age_days", 90)
    )
    http_connect_timeout_seconds = _env_float(
        "HTTP_CONNECT_TIMEOUT_SECONDS", _yaml_float(raw, "http_connect_timeout_seconds", 10)
    )
    http_read_timeout_seconds = _env_float(
        "HTTP_READ_TIMEOUT_SECONDS", _yaml_float(raw, "http_read_timeout_seconds", 20)
    )
    http_max_retries = _env_int("HTTP_MAX_RETRIES", _yaml_int(raw, "http_max_retries", 3))
    poll_max_workers = _env_int("POLL_MAX_WORKERS", _yaml_int(raw, "poll_max_workers", 5))

    if poll_interval_minutes < 1:
        raise ConfigError("poll_interval_minutes must be >= 1.")
    if body_truncate_bytes < 1024:
        raise ConfigError("body_truncate_bytes must be >= 1024.")
    if regex_timeout_seconds <= 0:
        raise ConfigError("regex_timeout_seconds must be > 0.")
    if output_item_cap < 1:
        raise ConfigError("output_item_cap must be >= 1.")
    if poll_max_workers < 1:
        raise ConfigError("poll_max_workers must be >= 1.")

    # --- Seed data ---
    source_seeds = _parse_source_seeds(raw.get("sources", []))
    keyword_seeds = _parse_keyword_seeds(raw.get("keywords", []))

    return AppConfig(
        admin_user=admin_user,
        admin_password_hash=admin_password_hash,
        secret_key=secret_key,
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        poll_interval_minutes=poll_interval_minutes,
        body_truncate_bytes=body_truncate_bytes,
        regex_timeout_seconds=regex_timeout_seconds,
        output_item_cap=output_item_cap,
        retention_max_matches=retention_max_matches,
        retention_max_age_days=retention_max_age_days,
        http_connect_timeout_seconds=http_connect_timeout_seconds,
        http_read_timeout_seconds=http_read_timeout_seconds,
        http_max_retries=http_max_retries,
        poll_max_workers=poll_max_workers,
        source_seeds=source_seeds,
        keyword_seeds=keyword_seeds,
    )


def _parse_source_seeds(items: Any) -> list[SourceSeed]:
    if not isinstance(items, list):
        raise ConfigError("config.yaml: 'sources' must be a list.")
    seeds: list[SourceSeed] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"config.yaml: sources[{i}] must be a mapping.")
        name = item.get("name")
        url = item.get("url")
        if not name or not isinstance(name, str):
            raise ConfigError(f"config.yaml: sources[{i}].name is required and must be a string.")
        if not url or not isinstance(url, str):
            raise ConfigError(f"config.yaml: sources[{i}].url is required and must be a string.")
        seeds.append(SourceSeed(name=name.strip(), url=url.strip(), enabled=_as_bool(item.get("enabled"))))
    return seeds


def _parse_keyword_seeds(items: Any) -> list[KeywordSeed]:
    if not isinstance(items, list):
        raise ConfigError("config.yaml: 'keywords' must be a list.")
    seeds: list[KeywordSeed] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"config.yaml: keywords[{i}] must be a mapping.")
        pattern = item.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ConfigError(f"config.yaml: keywords[{i}].pattern is required and must be a string.")
        # Validate that the seed regex compiles; a broken seed should fail fast.
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"config.yaml: keywords[{i}].pattern is not a valid regex: {exc}") from exc
        seeds.append(KeywordSeed(pattern=pattern, enabled=_as_bool(item.get("enabled"))))
    return seeds
