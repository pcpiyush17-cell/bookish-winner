"""Human-mediated sandbox authentication and restricted token storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_quant.clocks import Clock
from personal_quant.storage.database import StorageError

from .contracts import BrokerError, BrokerProfile
from .sandbox import KiteClient


class BrokerAuthenticationError(BrokerError):
    """Authentication failure whose message never contains credential values."""


@dataclass(frozen=True, slots=True)
class SandboxSession:
    profile: BrokerProfile
    token_path: Path


@dataclass(frozen=True, slots=True)
class StoredToken:
    access_token: str
    user_id: str
    authenticated_at: str


@dataclass(frozen=True, slots=True)
class TokenStore:
    path: Path

    def save(self, *, access_token: str, user_id: str, authenticated_at: str) -> Path:
        """Atomically store a session token outside source control with owner-only intent."""
        target = self.path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f"{target.suffix}.partial")
        payload = {
            "access_token": access_token,
            "user_id": user_id,
            "authenticated_at": authenticated_at,
        }
        try:
            partial.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.chmod(partial, 0o600)
            os.replace(partial, target)
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise StorageError(
                "token_store_failed", "Could not store sandbox session token"
            ) from error
        return target

    def load(self) -> StoredToken:
        """Load a stored token without displaying or logging its value."""
        target = self.path.expanduser().resolve()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            return StoredToken(
                access_token=_required_string(raw, "access_token"),
                user_id=_required_string(raw, "user_id"),
                authenticated_at=_required_string(raw, "authenticated_at"),
            )
        except (OSError, ValueError, json.JSONDecodeError, BrokerAuthenticationError):
            raise StorageError(
                "token_load_failed", "Could not load a valid sandbox session token"
            ) from None


@dataclass(frozen=True, slots=True)
class SandboxAuthenticator:
    client: KiteClient
    clock: Clock
    token_store: TokenStore

    def login_url(self) -> str:
        return self.client.login_url()

    def exchange(
        self, *, request_token: str, api_secret: str, expected_user_id: str
    ) -> SandboxSession:
        """Exchange a user-supplied request token, verify identity, and persist safely."""
        try:
            session = self.client.generate_session(request_token, api_secret=api_secret)
            access_token = _required_string(session, "access_token")
            self.client.set_access_token(access_token)
            raw_profile = self.client.profile()
            profile = BrokerProfile(
                user_id=_required_string(raw_profile, "user_id"),
                user_name=_required_string(raw_profile, "user_name"),
                broker=_required_string(raw_profile, "broker"),
                exchanges=tuple(str(value) for value in raw_profile["exchanges"]),
                products=tuple(str(value) for value in raw_profile["products"]),
            )
        except BrokerAuthenticationError:
            raise
        except Exception:
            raise BrokerAuthenticationError(
                "sandbox_auth_failed", "Sandbox authentication failed; credentials were redacted"
            ) from None
        if profile.user_id != expected_user_id:
            raise BrokerAuthenticationError(
                "sandbox_user_mismatch", "Authenticated sandbox user does not match expected user"
            )
        token_path = self.token_store.save(
            access_token=access_token,
            user_id=profile.user_id,
            authenticated_at=self.clock.now().isoformat(),
        )
        return SandboxSession(profile, token_path)


def redact_sensitive(value: object) -> object:
    """Recursively replace credential-bearing mapping fields with a fixed marker."""
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            redacted[key] = (
                "<redacted>"
                if any(word in normalized for word in ("token", "secret", "password"))
                else redact_sensitive(item)
            )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BrokerAuthenticationError(
            "sandbox_auth_response_invalid", "Sandbox authentication response was incomplete"
        )
    return value
