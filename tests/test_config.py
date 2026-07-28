from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_quant.config import ConfigurationError, OperatingMode, SystemConfig


def valid_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "application": {
            "name": "personal-quant",
            "timezone": "Asia/Kolkata",
            "log_level": "INFO",
            "data_root": Path("data"),
            "state_root": Path("state"),
            "report_root": Path("reports"),
        },
        "broker": {
            "provider": "zerodha",
            "api_key_env": "KITE_API_KEY",
            "api_secret_env": "KITE_API_SECRET",
            "expected_user_id_env": "KITE_EXPECTED_USER_ID",
            "access_token_path": Path("state/session/access_token.json"),
            "static_ip_required_for_orders": True,
            "expected_public_ip_env": "KITE_REGISTERED_PUBLIC_IP",
        },
        "market": {
            "exchange": "NSE",
            "segment": "equity",
            "product": "CNC",
            "currency": "INR",
        },
        "runtime": {
            "mode": "paper",
            "heartbeat_interval_seconds": 5,
            "stale_market_data_seconds": 15,
            "graceful_shutdown_seconds": 20,
            "lock_file": Path("state/locks/trading_engine.lock"),
        },
    }


def test_valid_config_is_frozen() -> None:
    config = SystemConfig.model_validate(valid_config())

    assert config.runtime.mode is OperatingMode.PAPER
    with pytest.raises(ValidationError, match="frozen"):
        config.runtime.mode = OperatingMode.LIVE


def test_runtime_mode_defaults_to_paper() -> None:
    raw = valid_config()
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    runtime.pop("mode")

    config = SystemConfig.model_validate(raw)

    assert config.runtime.mode is OperatingMode.PAPER


def test_unknown_field_fails_validation() -> None:
    raw = valid_config()
    raw["unexpected"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        SystemConfig.model_validate(raw)


def test_strict_types_reject_coercion() -> None:
    raw = valid_config()
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    runtime["heartbeat_interval_seconds"] = "5"

    with pytest.raises(ValidationError, match="int_type"):
        SystemConfig.model_validate(raw)


@pytest.mark.parametrize("timezone_name", ["Mars/Olympus", ""])
def test_invalid_timezone_fails_validation(timezone_name: str) -> None:
    raw = valid_config()
    application = raw["application"]
    assert isinstance(application, dict)
    application["timezone"] = timezone_name

    with pytest.raises(ValidationError, match="IANA timezone"):
        SystemConfig.model_validate(raw)


def test_invalid_environment_variable_name_fails_validation() -> None:
    raw = valid_config()
    broker = raw["broker"]
    assert isinstance(broker, dict)
    broker["api_key_env"] = "actual-secret-value"

    with pytest.raises(ValidationError, match="environment-variable"):
        SystemConfig.model_validate(raw)


def test_fingerprint_is_stable_across_mapping_order() -> None:
    first = SystemConfig.model_validate(valid_config())
    reordered = dict(reversed(list(valid_config().items())))
    second = SystemConfig.model_validate(reordered)

    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 64


def test_fingerprint_changes_with_validated_value() -> None:
    first = SystemConfig.model_validate(valid_config())
    raw = valid_config()
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    runtime["heartbeat_interval_seconds"] = 6
    second = SystemConfig.model_validate(raw)

    assert first.fingerprint() != second.fingerprint()


def test_example_yaml_loads() -> None:
    source = Path(__file__).parents[1] / "config" / "base.example.yaml"
    config = SystemConfig.from_yaml(source)

    assert config.runtime.mode is OperatingMode.PAPER


def test_yaml_loading_wraps_safe_structured_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ConfigurationError) as read_error:
        SystemConfig.from_yaml(missing)
    assert read_error.value.code == "config_read_failed"

    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("value: [", encoding="utf-8")
    with pytest.raises(ConfigurationError) as yaml_error:
        SystemConfig.from_yaml(invalid_yaml)
    assert yaml_error.value.code == "config_yaml_invalid"

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("hello", encoding="utf-8")
    with pytest.raises(ConfigurationError) as root_error:
        SystemConfig.from_yaml(scalar)
    assert root_error.value.code == "config_root_invalid"

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("schema_version: 1\nunknown: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as validation_error:
        SystemConfig.from_yaml(unknown)
    assert validation_error.value.code == "config_validation_failed"
    assert "input_value" not in str(validation_error.value)
