import time

import jwt
import pytest
from pydantic import SecretStr

from opsintel.auth import (
    ROLE_PERMISSIONS,
    AuthError,
    Permission,
    Principal,
    issue_token,
    verify_token,
)
from opsintel.config import Settings

S = Settings(jwt_secret=SecretStr("test-secret-0123456789-0123456789-xx"))


def test_round_trip() -> None:
    assert verify_token(issue_token("alice", "responder", settings=S), S) == Principal(
        "alice", "responder"
    )


def test_rejects_tampering_and_expiry() -> None:
    good = issue_token("alice", "viewer", settings=S)
    other = Settings(jwt_secret=SecretStr("another-secret-0123456789-0123456789"))
    with pytest.raises(AuthError):
        verify_token(good, other)
    expired = issue_token("alice", "viewer", ttl_seconds=-10, settings=S)
    with pytest.raises(AuthError, match="expired"):
        verify_token(expired, S)
    claims = jwt.decode(good, options={"verify_signature": False})
    claims["role"] = "admin"  # privilege escalation attempt without the key
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verify_token(unsigned, S)
    wrong_aud = jwt.encode(
        {**claims, "aud": "someone-else", "exp": int(time.time()) + 60},
        S.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_token(wrong_aud, S)


def test_unknown_role_rejected() -> None:
    with pytest.raises(AuthError):
        issue_token("x", "superuser", settings=S)
    with pytest.raises(AuthError):
        Principal("x", "root")


def test_role_hierarchy() -> None:
    viewer, responder, admin = (ROLE_PERMISSIONS[r] for r in ("viewer", "responder", "admin"))
    assert viewer < responder < admin
    assert Permission.READ_PII not in responder
    assert Permission.PROPOSE_ACTION not in viewer
