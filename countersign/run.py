"""The three operations: learn a form once, fill it under a grant, submit it once the
owner has countersigned the receipt.

`learn` walks the form in a throwaway sandbox (pressing Next through a form in steps)
and records its fields, its steps and a fingerprint. `fill` verifies the grant, refuses
anything out of scope before a container exists, then runs in a fresh sandbox and
returns a receipt and a screenshot of each step, without pressing Submit. `submit`
repeats the fill in another fresh sandbox, checks that it produces the receipt the
owner approved, applies the owner's own consent decisions, and only then presses Submit.

Which value goes in which field is decided before the sandbox starts: by exact match
here, or by the model (`mapper.py`), which labels each value known or guessed. Either
way the checks below are the same, and none of them is the model's.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import consent
from . import receipt as rc
from .fingerprint import drift, fingerprint
from .grant import Grant, check_scope, verify
from .policy import EgressPolicy
from .sandbox import Refused, Sandbox, SandboxKilled

MAX_STEPS = 8
# A button that moves a form in steps forward. Matched on its own label only.
NEXT = re.compile(r"^\s*(next|continue|proceed|weiter|siguiente|suivant)\b", re.I)


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
    fields: list[dict]  # each carries "step": the step at which it first shows
    fingerprint: str
    steps: tuple[str, ...] = ()  # the Next buttons, in order
    consent_kinds: dict = field(default_factory=dict)  # checkbox key -> (kind, label it was read from)


@dataclass(frozen=True)
class Approval:
    """The owner's countersignature: the receipt they read, and their decision for any
    consent box their grant left to them. Consent comes from the owner, here or as the
    standing rule in the grant, and from nowhere else."""

    digest: str
    consent: dict[str, bool] = field(default_factory=dict)


@dataclass
class Result:
    receipt: rc.Receipt
    screenshot_png: bytes | None = None  # the last step, as it would be submitted
    proxy_log: list[dict] = field(default_factory=list)
    landed: dict | None = None  # where Submit led, for a submitted run
    confirmation_png: bytes | None = None  # the page after Submit
    trail: list[tuple[str, bytes]] = field(default_factory=list)  # (caption, png) per step


@dataclass
class Learned:
    compiled: Compiled
    trail: list[tuple[str, bytes]]
    hidden_text: list[str]
    title: str


SandboxFactory = Callable[[EgressPolicy], Sandbox]
Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


def origin_of(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"{url!r} has no host")
    return host.lower()


def _shot(box: Sandbox) -> bytes:
    return base64.b64decode(box.call("screenshot")["png_b64"])


def _next_button(fields: list[dict], pressed: list[str]) -> str | None:
    if any(f["type"] == "submit" and f["visible"] for f in fields):
        return None  # the last step: what remains is Submit, which learning never presses
    for f in fields:
        if f["type"] == "button" and f["visible"] and f["key"] not in pressed and NEXT.match(f["label"] or ""):
            return f["key"]
    return None


def learn_walk(form_id: str, url: str, *, sandbox: SandboxFactory, progress: Progress = _quiet) -> Learned:
    """Walk the form to its last step without typing anything, and record what is there."""
    origin = origin_of(url)
    with sandbox(EgressPolicy(allow_hosts=frozenset({origin}))) as box:
        progress(f"sandbox {box.run_id} started; only {origin} is reachable")
        opened = box.call("open", url=url)
        page = box.call("read_form")
        first_seen: dict[str, int] = {}
        pressed: list[str] = []
        trail: list[tuple[str, bytes]] = []
        hidden: list[set[str]] = []
        for step in range(MAX_STEPS):
            for f in page["fields"]:
                if f["visible"]:
                    first_seen.setdefault(f["key"], step)
            hidden.append(set(page["hidden_text"]))
            trail.append((f"step {step + 1}", _shot(box)))
            nxt = _next_button(page["fields"], pressed)
            if nxt is None:
                break
            progress(f"step {step + 1}: pressing {nxt!r} to see the next step")
            box.call("click", key=nxt)
            pressed.append(nxt)
            page = box.call("read_form")
        # Visible means visible at some step: a field from step 1 is hidden by the last one.
        fields = [{**f, "step": first_seen.get(f["key"], 0), "visible": f["key"] in first_seen} for f in page["fields"]]
        quoted = [t for t in page["hidden_text"] if all(t in h for h in hidden)]
    if not fields:
        raise ValueError(f"no form controls found at {url}")
    progress(f"read {len(fields)} controls over {len(pressed) + 1} step(s)")
    return Learned(Compiled(form_id, url, origin, fields, fingerprint(fields), tuple(pressed)), trail, quoted,
                   opened.get("title") or "")


def learn(form_id: str, url: str, *, sandbox: SandboxFactory) -> Compiled:
    return learn_walk(form_id, url, sandbox=sandbox).compiled


def exact_mapping(fields: list[dict], facts: dict[str, str]) -> dict[str, rc.Intent]:
    keys = {f["key"] for f in fields}
    return {k: rc.Intent(str(v), f"fact:{k}") for k, v in facts.items() if k in keys}


def _admit(compiled, facts, grant_token, issuer, intents, now, *, submit) -> tuple[Grant, dict[str, rc.Intent]]:
    """Everything that is decided before a container exists."""
    grant = verify(issuer, grant_token, now=now)
    intents = exact_mapping(compiled.fields, facts) if intents is None else intents
    disclosed = sorted(set().union(*(i.facts_used() for i in intents.values())) if intents else set())
    refusals = check_scope(grant, form_id=compiled.form_id, origin=compiled.origin, facts=disclosed, submit=submit)
    if refusals:
        raise OutOfScope(refusals)
    return grant, intents


def _consent_decision(compiled: Compiled, f: dict) -> bool | None:
    """The standing rule for one visible box. The model's reading from learning time is
    used only while the box still says what it said then; the patterns always run on
    the live label."""
    learned = compiled.consent_kinds.get(f["key"])
    model_kind = learned[0] if learned and learned[1] == (f["label"] or "") else None
    return consent.decide(f, model_kind)


def _fill_page(box: Sandbox, compiled: Compiled, intents: dict[str, rc.Intent], consent_policy: str = "ask", *,
               shots: bool = False, progress: Progress = _quiet) -> dict:
    box.call("open", url=compiled.url)
    page = box.call("read_form")
    page["drift"] = drift(compiled.fields, page["fields"])
    page["consent"], page["consent_was"], page["trail"] = {}, {}, []
    if page["drift"]:
        if shots:
            page["trail"].append(("as found", _shot(box)))
        return page  # a form that changed shape is reported, not filled
    found_drift = page["drift"]
    step_of = {f["key"]: f.get("step", 0) for f in compiled.fields}
    was, want, trail = {}, {}, []
    seen_visible: set[str] = set()
    hidden: list[set[str]] = []
    for step in range(len(compiled.steps) + 1):
        if step:
            box.call("click", key=compiled.steps[step - 1])
            page = box.call("read_form")
        seen_visible |= {f["key"] for f in page["fields"] if f["visible"]}
        hidden.append(set(page["hidden_text"]))
        if consent_policy == "accept-terms":
            for f in page["fields"]:
                if f["type"] != "checkbox" or not f["visible"] or f["key"] in want:
                    continue
                decision = _consent_decision(compiled, f)
                if decision is not None:
                    was[f["key"]], want[f["key"]] = f["checked"], decision
                    if f["checked"] != decision:
                        box.call("set_consent", key=f["key"], checked=decision)
        filled = 0
        for key, intent in intents.items():
            if step_of.get(key, 0) != step:
                continue
            try:
                box.call("fill", key=key, value=intent.value)
                filled += 1
            except Refused:
                pass  # the receipt reports it: the read-back will not match, or the field is "yours"
        progress(f"step {step + 1}: filled {filled} field(s)")
        if shots:
            trail.append((f"step {step + 1} filled", _shot(box)))
    after = box.call("read_form")
    for f in after["fields"]:
        # A field shown at an earlier step is hidden now, and was seen when it was filled.
        f["visible"] = f["visible"] or f["key"] in seen_visible
    after["hidden_text"] = [t for t in after["hidden_text"] if all(t in h for h in hidden)]
    after["drift"], after["consent"], after["consent_was"], after["trail"] = found_drift, want, was, trail
    return after


def _receipt(box: Sandbox, compiled: Compiled, grant: Grant, intents, page: dict | None, killed: str | None = None) -> rc.Receipt:
    """Built from the proxy log as it stands: complete after a run, so far during one."""
    page = page or {"fields": [], "hidden_text": [], "total_text": None, "drift": None, "consent": {}, "consent_was": {}}
    return rc.build(
        run_id=box.run_id, form_id=compiled.form_id, caller=grant.caller, grant_id=grant.grant_id,
        origin=compiled.origin, after=page["fields"], intents={} if page["drift"] else intents,
        blocked=box.blocked(), hidden_text=page["hidden_text"], total_text=page["total_text"],
        max_total=grant.max_total, drift=page["drift"] or None, killed=killed,
        consent=page["consent"], consent_was=page["consent_was"], consent_policy=grant.consent,
    )


def fill(compiled: Compiled, facts: dict[str, str], grant_token: str, issuer: Ed25519PublicKey, *,
         sandbox: SandboxFactory, intents: dict[str, rc.Intent] | None = None, now: float | None = None,
         progress: Progress = _quiet) -> Result:
    grant, intents = _admit(compiled, facts, grant_token, issuer, intents, now, submit=False)
    box = sandbox(EgressPolicy(allow_hosts=frozenset(grant.origins)))  # the grant's origins and nothing else
    page, killed = None, None
    try:
        with box:
            progress(f"sandbox {box.run_id} started; reachable: {', '.join(grant.origins)}")
            page = _fill_page(box, compiled, intents, grant.consent, shots=True, progress=progress)
            if not page["trail"]:
                page["trail"].append(("as found", _shot(box)))
    except SandboxKilled as exc:
        killed = str(exc)
        progress(f"sandbox destroyed: {killed}")
    progress("sandbox, proxy and network removed")
    trail = (page or {}).get("trail") or []
    shot = trail[-1][1] if trail else None
    return Result(_receipt(box, compiled, grant, intents, page, killed), shot, box.proxy_log(), trail=trail)


def submit(compiled: Compiled, facts: dict[str, str], grant_token: str, issuer: Ed25519PublicKey, approval: Approval, *,
           sandbox: SandboxFactory, intents: dict[str, rc.Intent] | None = None, now: float | None = None,
           progress: Progress = _quiet) -> Result:
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
        progress(f"second sandbox {box.run_id} started; replaying the approved fill")
        page = _fill_page(box, compiled, intents, grant.consent, progress=progress)
        receipt = _receipt(box, compiled, grant, intents, page)
        if receipt.digest() != approval.digest:
            raise NotApproved("this run does not produce the receipt that was approved; read the new one first")
        progress("replay matches the receipt you approved")
        # The only block an owner can lift is a consent box, by deciding it themselves.
        liftable = ("consent-preticked", "consent-required")
        open_blocks = [f for f in receipt.flags if f.blocking and not (f.kind in liftable and f.key in approval.consent)]
        if open_blocks:
            raise NotApproved("; ".join(f"{f.kind}: {f.detail}" for f in open_blocks))
        for key, checked in sorted(approval.consent.items()):
            box.call("set_consent", key=key, checked=bool(checked))
        state = {f["key"]: f for f in box.call("read_form")["fields"]}
        wrong = [k for k, v in approval.consent.items() if state[k]["checked"] != bool(v)]
        moved = [k for k, i in intents.items() if k in state and not rc.is_yours(state[k]) and state[k]["value"] != i.value]
        if wrong or moved:
            raise NotApproved(f"the page did not hold the approved state: {', '.join(sorted(wrong + moved))}")
        before = _shot(box)
        progress("pressing Submit")
        try:
            landed = box.call("click", key=buttons[0], submit=True)
            after = _shot(box)
        except Refused:
            raise
        except Exception as exc:
            raise SubmitUncertain(
                f"Submit was pressed on {compiled.url} and the run failed before the result was read ({exc}). "
                "Check with the site before trying again."
            ) from exc
        receipt.submitted = True
    progress("second sandbox removed")
    return Result(receipt, before, box.proxy_log(), {"url": landed.get("url"), "title": landed.get("title")}, after,
                  trail=[("before Submit", before), ("after Submit", after)])
