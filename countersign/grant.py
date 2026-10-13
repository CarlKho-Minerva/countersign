"""Signed, scoped, expiring grants.

A grant is the owner's statement of what one caller may do with one form: which
origins the sandbox may reach, which facts may be disclosed, the largest total that
may be accepted, and whether submitting is allowed at all. The control plane checks
it before a container starts, so an out-of-scope call never reaches a sandbox.

Token: base64url(canonical JSON) "." base64url(Ed25519 signature over those bytes).
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_TOKEN_BYTES = 8192


class GrantError(Exception):
    """The token cannot be trusted: malformed, forged, or expired."""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    caller: str
    form_id: str
    origins: tuple[str, ...]
    facts: tuple[str, ...]
    max_total: Decimal | None
    may_submit: bool
    expires_at: int

    def total_ok(self, total: Decimal) -> bool:
        return self.max_total is None or total <= self.max_total


def new_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(private_key: Ed25519PrivateKey, grant: Grant) -> str:
    body = asdict(grant)
    body["max_total"] = None if grant.max_total is None else str(grant.max_total)
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return _b64(payload) + "." + _b64(private_key.sign(payload))


def verify(public_key: Ed25519PublicKey, token: str, now: float | None = None) -> Grant:
    if not token or len(token) > MAX_TOKEN_BYTES or token.count(".") != 1:
        raise GrantError("malformed grant token")
    head, sig = token.split(".")
    try:
        payload, signature = _unb64(head), _unb64(sig)
    except (binascii.Error, ValueError) as exc:
        raise GrantError("malformed grant token") from exc
    try:
        public_key.verify(signature, payload)
    except InvalidSignature as exc:
        raise GrantError("bad signature on grant") from exc
    # Only parsed after the signature holds, so nothing unsigned is ever interpreted.
    try:
        body = json.loads(payload)
        grant = Grant(
            grant_id=str(body["grant_id"]),
            caller=str(body["caller"]),
            form_id=str(body["form_id"]),
            origins=tuple(str(o) for o in body["origins"]),
            facts=tuple(str(f) for f in body["facts"]),
            max_total=None if body["max_total"] is None else Decimal(body["max_total"]),
            may_submit=body["may_submit"] is True,
            expires_at=int(body["expires_at"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise GrantError("malformed grant body") from exc
    if (time.time() if now is None else now) >= grant.expires_at:
        raise GrantError(f"grant {grant.grant_id} expired")
    return grant


def check_scope(
    grant: Grant, *, form_id: str, origin: str, facts: list[str], submit: bool = False
) -> list[str]:
    """Every way this call exceeds the grant. Empty means in scope."""
    refusals = []
    if form_id != grant.form_id:
        refusals.append(f"form: grant {grant.grant_id} is for {grant.form_id}, not {form_id}")
    if origin not in grant.origins:
        refusals.append(f"origin: {origin} is not in grant {grant.grant_id}")
    for fact in facts:
        if fact not in grant.facts:
            refusals.append(f"fact: {fact} may not be disclosed under grant {grant.grant_id}")
    if submit and not grant.may_submit:
        refusals.append(f"submit: grant {grant.grant_id} does not allow submitting")
    return refusals
