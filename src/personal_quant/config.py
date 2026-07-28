"""Strict, immutable application configuration and reproducibility hashing."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigurationError(ValueError):
    """A safe configuration-loading failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OperatingMode(StrEnum):
    BACKTEST = "backtest"
    REPLAY = "replay"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Exchange(StrEnum):
    NSE = "NSE"


class MarketSegment(StrEnum):
    EQUITY = "equity"


class Product(StrEnum):
    CNC = "CNC"


class StrictModel(BaseModel):
    """Base for immutable configs that reject coercion and unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplicationConfig(StrictModel):
    name: Annotated[StrictStr, Field(min_length=1)]
    timezone: StrictStr
    log_level: LogLevel
    data_root: Path
    state_root: Path
    report_root: Path

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value


class BrokerConfig(StrictModel):
    provider: Annotated[StrictStr, Field(min_length=1)]
    api_key_env: StrictStr
    api_secret_env: StrictStr
    expected_user_id_env: StrictStr
    access_token_path: Path
    static_ip_required_for_orders: StrictBool
    expected_public_ip_env: StrictStr

    @field_validator(
        "api_key_env",
        "api_secret_env",
        "expected_user_id_env",
        "expected_public_ip_env",
    )
    @classmethod
    def validate_environment_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment-variable name")
        return value


class MarketConfig(StrictModel):
    exchange: Exchange
    segment: MarketSegment
    product: Product
    currency: Literal["INR"]


class RuntimeConfig(StrictModel):
    mode: OperatingMode = OperatingMode.PAPER
    heartbeat_interval_seconds: Annotated[StrictInt, Field(gt=0)]
    stale_market_data_seconds: Annotated[StrictInt, Field(gt=0)]
    graceful_shutdown_seconds: Annotated[StrictInt, Field(gt=0)]
    lock_file: Path


class SystemConfig(StrictModel):
    schema_version: Annotated[StrictInt, Field(ge=1)]
    application: ApplicationConfig
    broker: BrokerConfig
    market: MarketConfig
    runtime: RuntimeConfig

    def fingerprint(self) -> str:
        """Return a SHA-256 hash of canonical, validated configuration data."""
        serializable = self.model_dump(mode="json")
        canonical = json.dumps(serializable, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        """Load a YAML file without resolving or exposing secret values."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError("config_read_failed", f"Cannot read config: {path}") from error

        try:
            raw: Any = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ConfigurationError(
                "config_yaml_invalid", "Configuration YAML is invalid"
            ) from error

        if not isinstance(raw, dict):
            raise ConfigurationError("config_root_invalid", "Configuration root must be a mapping")

        try:
            return cls.model_validate(raw)
        except ValidationError as error:
            failures = error.errors(include_input=False, include_url=False)
            summary = "; ".join(
                f"{'.'.join(str(part) for part in failure['loc'])}: {failure['msg']}"
                for failure in failures
            )
            raise ConfigurationError(
                "config_validation_failed", f"Configuration validation failed: {summary}"
            ) from error
