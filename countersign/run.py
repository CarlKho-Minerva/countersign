"""The three operations: learn a form once, fill it under a grant, submit it once the
owner has countersigned the receipt.

`learn` compiles a form into its fields and a fingerprint. `fill` verifies the grant,
refuses anything out of scope before a container exists, then runs in a fresh sandbox
and returns a receipt without pressing Submit. `submit` repeats the fill in another
fresh sandbox, checks that it produces the receipt the owner approved, applies the
owner's own consent decisions, and only then presses Submit.

The mapping from facts to fields is exact-match for now. From kickoff the model
proposes the mapping (and labels its guesses); the checks around it stay the same.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import receipt as rc
from .fingerprint import drift, fingerprint
from .grant import Grant, check_scope, verify
from .policy import EgressPolicy
from .sandbox import Refused, Sandbox, SandboxKilled


class OutOfScope(Exception):
    """The call exceeds its grant. No sandbox was started."""

    def __init__(self, refusals: list[str]):
        super().__init__("; ".join(refusals))
        self.refusals = refusals


class NotApproved(Exception):
    """The approval does not cover what this run would submit. Nothing was submitted."""


class SubmitUncertain(Exception):
    """Submit was pressed and the run failed before the outcome was read. The form may
    or may not have gone through. Never retried automatically."""


@dataclass(frozen=True)
class Compiled:
    form_id: str
    url: str
    origin: str
    fields: list[dict]
    fingerprint: str


@dataclass(frozen=True)
class Approval:
    """The owner's countersignature: the receipt they read, and their decision for
    each consent box on it. Consent comes from here and from nowhere else."""

    digest: str
    consent: dict[str, bool] = field(default_factory=dict)


@dataclass
class Result:
    receipt: rc.Receipt
    screenshot_png: bytes | None = None
    proxy_log: list[dict] = field(default_factory=list)
    landed: dict | None = None  # where Submit led, for a submitted run
    confirmation_png: bytes | None = None  # the page after Submit


SandboxFactory = Callable[[EgressPolicy], Sandbox]


def origin_of(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"{url!r} has no host")
    return host.lower()


def learn(form_id: str, url: str, *, sandbox: SandboxFactory) -> Compiled:
    origin = origin_of(url)
    with sandbox(EgressPolicy(allow_hosts=frozenset({origin}))) as box:
        box.call("open", url=url)
        fields = box.call("read_form")["fields"]
    if not fields:
        raise ValueError(f"no form controls found at {url}")
    return Compiled(form_id, url, origin, fields, fingerprint(fields))


def exact_mapping(fields: list[dict], facts: dict[str, str]) -> dict[str, rc.Intent]:
    keys = {f["key"] for f in fields}
    return {k: rc.Intent(str(v), f"fact:{k}") for k, v in facts.items() if k in keys}


def _admit(compiled, facts, grant_token, issuer, intents, now, *, submit) -> tuple[Grant, dict[str, rc.Intent]]:
    """Everything that is decided before a container exists."""
    grant = verify(issuer, grant_token, now=now)
    intents = exact_mapping(compiled.fields, facts) if intents is None else intents
    disclosed = sorted({i.source.split(":", 1)[1] for i in intents.values() if i.known})
    refusals = check_scope(grant, form_id=compiled.form_id, origin=compiled.origin, facts=disclosed, submit=submit)
    if refusals:
        raise OutOfScope(refusals)
    return grant, intents


def _fill_page(box: Sandbox, compiled: Compiled, intents: dict[str, rc.Intent]) -> dict:
    box.call("open", url=compiled.url)
    page = box.call("read_form")
    page["drift"] = drift(compiled.fields, page["fields"])
    if page["drift"]:
        return page  # a form that changed shape is reported, not filled
    for key, intent in intents.items():
        try:
            box.call("fill", key=key, value=intent.value)
        except Refused:
            pass  # the receipt reports it: the read-back will not match, or the field is "yours"
    after = box.call("read_form")
    after["drift"] = page["drift"]
    return after


def _receipt(box: Sandbox, compiled: Compiled, grant: Grant, intents, page: dict | None, killed: str | None = None) -> rc.Receipt:
    """Built from the proxy log as it stands: complete after a run, so far during one."""
    page = page or {"fields": [], "hidden_text": [], "total_text": None, "drift": None}
    return rc.build(
        run_id=box.run_id, form_id=compiled.form_id, caller=grant.caller, grant_id=grant.grant_id,
        origin=compiled.origin, after=page["fields"], intents={} if page["drift"] else intents,
        blocked=box.blocked(), hidden_text=page["hidden_text"], total_text=page["total_text"],
        max_total=grant.max_total, drift=page["drift"] or None, killed=killed,
    )


def fill(compiled: Compiled, facts: dict[str, str], grant_token: str, issuer: Ed25519PublicKey, *,
         sandbox: SandboxFactory, intents: dict[str, rc.Intent] | None = None, now: float | None = None) -> Result:
    grant, intents = _admit(compiled, facts, grant_token, issuer, intents, now, submit=False)
    box = sandbox(EgressPolicy(allow_hosts=frozenset(grant.origins)))  # the grant's origins and nothing else
    page, shot, killed = None, None, None
    try:
        with box:
            page = _fill_page(box, compiled, intents)
            shot = base64.b64decode(box.call("screenshot")["png_b64"])
    except SandboxKilled as exc:
        killed = str(exc)
    return Result(_receipt(box, compiled, grant, intents, page, killed), shot, box.proxy_log())


def submit(compiled: Compiled, facts: dict[str, str], grant_token: str, issuer: Ed25519PublicKey, approval: Approval, *,
           sandbox: SandboxFactory, intents: dict[str, rc.Intent] | None = None, now: float | None = None) -> Result:
    grant, intents = _admit(compiled, facts, grant_token, issuer, intents, now, submit=True)
    buttons = [f["key"] for f in compiled.fields if f["type"] == "submit"]
    if len(buttons) != 1:
        raise NotApproved(f"the form has {len(buttons)} submit controls; which one to press was never learned")
    checkboxes = {f["key"] for f in compiled.fields if f["type"] == "checkbox"}
    stray = sorted(set(approval.consent) - checkboxes)
    if stray:
        raise NotApproved(f"the approval decides {', '.join(stray)}, which are not consent boxes on this form")

    box = sandbox(EgressPolicy(allow_hosts=frozenset(grant.origins)))
    with box:
        page = _fill_page(box, compiled, intents)
        receipt = _receipt(box, compiled, grant, intents, page)
        if receipt.digest() != approval.digest:
            raise NotApproved("this run does not produce the receipt that was approved; read the new one first")
        # The only block an owner can lift is a consent box, by deciding it themselves.
        open_blocks = [f for f in receipt.flags if f.blocking and not (f.kind == "consent-preticked" and f.key in approval.consent)]
        if open_blocks:
            raise NotApproved("; ".join(f"{f.kind}: {f.detail}" for f in open_blocks))
        for key, checked in sorted(approval.consent.items()):
            box.call("set_consent", key=key, checked=bool(checked))
        state = {f["key"]: f for f in box.call("read_form")["fields"]}
        wrong = [k for k, v in approval.consent.items() if state[k]["checked"] != bool(v)]
        moved = [k for k, i in intents.items() if k in state and not rc.is_yours(state[k]) and state[k]["value"] != i.value]
        if wrong or moved:
            raise NotApproved(f"the page did not hold the approved state: {', '.join(sorted(wrong + moved))}")
        before = base64.b64decode(box.call("screenshot")["png_b64"])
        try:
            landed = box.call("click", key=buttons[0], submit=True)
            after = base64.b64decode(box.call("screenshot")["png_b64"])
        except Refused:
            raise
        except Exception as exc:
            raise SubmitUncertain(
                f"Submit was pressed on {compiled.url} and the run failed before the result was read ({exc}). "
                "Check with the site before trying again."
            ) from exc
        receipt.submitted = True
    return Result(receipt, before, box.proxy_log(), {"url": landed.get("url"), "title": landed.get("title")}, after)

