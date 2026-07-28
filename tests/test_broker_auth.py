import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_quant.broker.auth import (
    BrokerAuthenticationError,
    SandboxAuthenticator,
    TokenStore,
    redact_sensitive,
)
from personal_quant.clocks import SimulatedClock

from .test_sandbox_adapter import FakeKiteClient


def test_sandbox_auth_verifies_user_and_stores_token(tmp_path: Path) -> None:
    client = FakeKiteClient()
    current = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    token_path = tmp_path / "state" / "sandbox_token.json"
    authenticator = SandboxAuthenticator(client, SimulatedClock(current), TokenStore(token_path))

    assert "sandbox.kite.trade" in authenticator.login_url()
    result = authenticator.exchange(
        request_token="short-lived", api_secret="fixture-secret", expected_user_id="SANDBOX01"
    )

    stored = json.loads(token_path.read_text(encoding="utf-8"))
    assert result.profile.user_id == "SANDBOX01"
    assert stored["access_token"] == "fixture-access"
    assert stored["authenticated_at"] == current.isoformat()


def test_sandbox_auth_rejects_account_mismatch_without_writing(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    authenticator = SandboxAuthenticator(
        FakeKiteClient(),
        SimulatedClock(datetime(2026, 7, 28, 10, 0, tzinfo=UTC)),
        TokenStore(token_path),
    )

    with pytest.raises(BrokerAuthenticationError) as error:
        authenticator.exchange(
            request_token="short-lived",
            api_secret="fixture-secret",
            expected_user_id="WRONGUSER",
        )
    assert error.value.code == "sandbox_user_mismatch"
    assert not token_path.exists()


def test_redaction_is_recursive_and_does_not_echo_values() -> None:
    raw = {
        "access_token": "sensitive-one",
        "nested": {"api_secret": "sensitive-two", "safe": "visible"},
        "items": [{"password": "sensitive-three"}],
    }

    redacted = redact_sensitive(raw)
    rendered = repr(redacted)

    assert "sensitive" not in rendered
    assert "visible" in rendered
    assert rendered.count("<redacted>") == 3


def test_redacted_fixtures_contain_no_token_values() -> None:
    fixture = Path(__file__).parent / "fixtures" / "broker" / "sandbox_profile.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))

    assert raw["access_token"] == "<redacted>"
    assert raw["api_secret"] == "<redacted>"
