"""Identity and role-based access control.

Permissions are checked in the tool layer on every call, using the identity of the human
who started the investigation. The model never sees or chooses its own role, so no prompt
can widen what a tool returns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

import jwt

from opsintel.config import Settings, get_settings


class Permission(StrEnum):
    READ_TELEMETRY = "read:telemetry"  # events, metrics, changes, service catalogue
    READ_GRAPH = "read:graph"
    READ_DOCS = "read:docs"  # further filtered by each document's ACL
    READ_ORDERS = "read:orders"  # order/payment aggregates keyed by customer ID only
    READ_PII = "read:pii"  # customer names, emails, phone numbers
    PROPOSE_ACTION = "propose:action"
    DECIDE_ACTION = "decide:action"  # approve or reject proposals (phase 6)


_VIEWER = {
    Permission.READ_TELEMETRY,
    Permission.READ_GRAPH,
    Permission.READ_DOCS,
    Permission.READ_ORDERS,
}
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset(_VIEWER),
    "responder": frozenset(_VIEWER | {Permission.PROPOSE_ACTION, Permission.DECIDE_ACTION}),
    "admin": frozenset(Permission),
}


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLE_PERMISSIONS:
            raise AuthError(f"unknown role {self.role!r}")

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


def issue_token(
    subject: str, role: str, ttl_seconds: int = 3600, settings: Settings | None = None
) -> str:
    s = settings or get_settings()
    Principal(subject, role)  # validates the role
    now = int(time.time())
    claims = {
        "sub": subject,
        "role": role,
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm="HS256")


def verify_token(token: str, settings: Settings | None = None) -> Principal:
    s = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=["HS256"],  # pinned: never trust the token's own alg header
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            options={"require": ["sub", "role", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}") from e
    return Principal(subject=str(claims["sub"]), role=str(claims["role"]))
