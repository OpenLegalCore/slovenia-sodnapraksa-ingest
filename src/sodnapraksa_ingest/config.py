"""Fail-closed configuration without production defaults."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse


class ConfigError(RuntimeError):
    """Invalid or unsafe configuration."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    expected_database: str
    expected_schema: str
    source_base_url: str
    source_api_key: str = field(repr=False)
    qdrant_url: str
    qdrant_api_key: str = field(repr=False)
    qdrant_collection: str
    embedding_base_url: str
    embedding_api_key: str = field(repr=False)
    embedding_model: str
    embedding_dimensions: int
    checkpoint_path: Path
    lock_path: Path
    initial_since: datetime
    overlap: timedelta
    discovery_limit: int
    document_limit: int
    embedding_byte_cap: int
    allow_external_api: bool
    allow_writes: bool


def load_settings(mode: str, env: Mapping[str, str] | None = None) -> Settings:
    """Parse all settings before opening a file or network connection."""

    if mode not in {"preflight", "run"}:
        raise ConfigError("mode must be preflight or run")
    source = os.environ if env is None else env

    def required(name: str) -> str:
        value = str(source.get(name, "")).strip()
        if not value or (value.startswith("<") and value.endswith(">")):
            raise ConfigError(f"required configuration is missing: {name}")
        return value

    def identifier(name: str, *, qdrant: bool = False) -> str:
        value = required(name)
        pattern = r"[A-Za-z0-9_.-]+" if qdrant else r"[a-z_][a-z0-9_]*"
        if not re.fullmatch(pattern, value):
            raise ConfigError(f"invalid identifier: {name}")
        return value

    def positive(name: str, maximum: int | None = None) -> int:
        try:
            value = int(required(name))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a positive integer") from exc
        if value <= 0 or (maximum is not None and value > maximum):
            raise ConfigError(f"{name} is outside the allowed range")
        return value

    def flag(name: str) -> bool:
        value = required(name)
        if value not in {"0", "1"}:
            raise ConfigError(f"{name} must be 0 or 1")
        return value == "1"

    def absolute(name: str) -> Path:
        path = Path(required(name))
        if not path.is_absolute():
            raise ConfigError(f"{name} must be absolute")
        return path

    def endpoint(name: str, *, https_only: bool) -> str:
        value = required(name)
        parsed = urlparse(value)
        schemes = {"https"} if https_only else {"http", "https"}
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError(f"invalid endpoint: {name}")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ConfigError(f"unencrypted endpoint is not loopback: {name}")
        return value.rstrip("/")

    expected_database = identifier("SODNAPRAKSA_EXPECTED_DATABASE")
    database_url = required("SODNAPRAKSA_DATABASE_URL")
    dsn = urlparse(database_url)
    if dsn.scheme not in {"postgres", "postgresql"} or not dsn.hostname or unquote(dsn.path.lstrip("/")) != expected_database:
        raise ConfigError("PostgreSQL DSN does not match the expected database")
    try:
        initial_since = datetime.fromisoformat(required("SODNAPRAKSA_INITIAL_SINCE").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError("SODNAPRAKSA_INITIAL_SINCE must be ISO-8601") from exc
    if initial_since.tzinfo is None:
        raise ConfigError("SODNAPRAKSA_INITIAL_SINCE must include a timezone")
    checkpoint = absolute("SODNAPRAKSA_CHECKPOINT_PATH")
    lock = absolute("SODNAPRAKSA_LOCK_PATH")
    if checkpoint == lock:
        raise ConfigError("checkpoint and lock paths must differ")
    external = flag("SODNAPRAKSA_ALLOW_EXTERNAL_API")
    writes = flag("SODNAPRAKSA_ALLOW_WRITES")
    if mode == "preflight" and (external or writes):
        raise ConfigError("preflight requires both authorization flags to be 0")
    if mode == "run" and not (external and writes):
        raise ConfigError("run requires both authorization flags to be 1")
    qdrant_key = str(source.get("SODNAPRAKSA_QDRANT_API_KEY", "")).strip()
    if qdrant_key.startswith("<") and qdrant_key.endswith(">"):
        raise ConfigError("replace or clear SODNAPRAKSA_QDRANT_API_KEY")
    return Settings(
        database_url=database_url,
        expected_database=expected_database,
        expected_schema=identifier("SODNAPRAKSA_EXPECTED_SCHEMA"),
        source_base_url=endpoint("SODNAPRAKSA_SOURCE_BASE_URL", https_only=True),
        source_api_key=required("SODNAPRAKSA_SOURCE_API_KEY"),
        qdrant_url=endpoint("SODNAPRAKSA_QDRANT_URL", https_only=False),
        qdrant_api_key=qdrant_key,
        qdrant_collection=identifier("SODNAPRAKSA_QDRANT_COLLECTION", qdrant=True),
        embedding_base_url=endpoint("SODNAPRAKSA_EMBEDDING_BASE_URL", https_only=True),
        embedding_api_key=required("SODNAPRAKSA_EMBEDDING_API_KEY"),
        embedding_model=required("SODNAPRAKSA_EMBEDDING_MODEL"),
        embedding_dimensions=positive("SODNAPRAKSA_EMBEDDING_DIMENSIONS", 65536),
        checkpoint_path=checkpoint,
        lock_path=lock,
        initial_since=initial_since.astimezone(UTC).replace(microsecond=0),
        overlap=timedelta(days=positive("SODNAPRAKSA_OVERLAP_DAYS", 31)),
        discovery_limit=positive("SODNAPRAKSA_DISCOVERY_LIMIT"),
        document_limit=positive("SODNAPRAKSA_DOCUMENT_LIMIT"),
        embedding_byte_cap=positive("SODNAPRAKSA_MAX_EMBEDDING_INPUT_BYTES_PER_RUN"),
        allow_external_api=external,
        allow_writes=writes,
    )
