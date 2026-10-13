"""The receipt: what a run did to a form, stated so a person can approve or reject it.

Built from facts the sandbox reported (the form before and after the fill, the proxy's
log) and from what the control plane intended. Nothing here trusts page text: hidden
text is quoted as evidence and never acted on.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from .fingerprint import Drift

# Fields a run never fills. They are listed on the receipt as the owner's to decide.
_SECRET_TYPES = {"password"}
_PAYMENT = re.compile(r"(card|cc-|cvv|cvc|iban|routing|account.?number)", re.I)
_AMOUNT = re.compile(r"(-?)\s*(?:[$€£]|USD|EUR|GBP)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
MAX_QUOTE = 400


@dataclass(frozen=True)
class Intent:
    """A value the control plane decided to put in a field, and why."""

    value: str
    source: str  # "fact:<key>" for a value the owner supplied, "guess:<reason>" otherwise

    @property
    def known(self) -> bool:
        return self.source.startswith("fact:")


@dataclass
class Entry:
    key: str
    label: str
    value: str
    source: str  # fact:<key> | guess:<reason> | policy:<name> | page-default | none
    status: str  # known | guess | policy | yours | unreviewed | empty


@dataclass
class Flag:
    kind: str
    detail: str
    blocking: bool
    key: str = ""  # the field this is about, when it is about one


@dataclass
class Receipt:
    run_id: str
    form_id: str
    caller: str
    grant_id: str
    origin: str
    entries: list[Entry] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    total: str | None = None
    submitted: bool = False

    @property
    def approvable(self) -> bool:
        return not any(f.blocking for f in self.flags)

    def digest(self) -> str:
        """Binds an approval to the state of the form: the submit replay must match it.

        Blocked requests are left out. They are evidence about the sandbox, their number
        depends on timing, and they do not change what would be submitted.
        """
        body = asdict(self)
        body.pop("run_id")
        body.pop("submitted")
        body["flags"] = [f for f in body["flags"] if f["kind"] != "egress-blocked"]
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {**asdict(self), "approvable": self.approvable, "digest": self.digest()}


def parse_amount(text: str | None) -> Decimal | None:
    """The last money amount in a string like 'Order Total: $1,278.00'."""
    if not text:
        return None
    hits = _AMOUNT.findall(text)
    if not hits:
        return None
    sign, whole, cents = hits[-1]
    try:
        return Decimal(f"{sign}{whole.replace(',', '')}.{cents or '00'}")
    except InvalidOperation:
        return None


def is_yours(f: dict) -> bool:
    if f["type"] in _SECRET_TYPES or f["type"] == "checkbox":
        return True
    return bool(_PAYMENT.search(f["key"]) or _PAYMENT.search(f.get("autocomplete") or ""))


def _shown(f: dict) -> str:
    if f["type"] in ("checkbox", "radio"):
        return "ticked" if f["checked"] else "not ticked"
    return "(hidden)" if f["type"] in _SECRET_TYPES and f["value"] else f["value"]


def build(
    *,
    run_id: str,
    form_id: str,
    caller: str,
    grant_id: str,
    origin: str,
    after: list[dict],
    intents: dict[str, Intent],
    blocked: list[dict],
    hidden_text: list[str],
    total_text: str | None,
    max_total: Decimal | None,
    drift: Drift | None = None,
    killed: str | None = None,
    consent: dict[str, bool] | None = None,
    consent_was: dict[str, bool] | None = None,
    consent_policy: str = "ask",
) -> Receipt:
    r = Receipt(run_id=run_id, form_id=form_id, caller=caller, grant_id=grant_id, origin=origin)

    if drift:
        r.flags.append(Flag("drift", "form changed shape since it was learned: " + "; ".join(drift.lines()), True))
    if killed:
        r.flags.append(Flag("killed", killed, True))

    consent, consent_was = consent or {}, consent_was or {}
    seen = set()
    for f in after:
        if f["type"] == "checkbox" and f["checked"] and not f["visible"]:
            # It would be submitted with the form, and nobody could have seen it.
            r.flags.append(Flag("hidden-consent", f"{f['key']} is a ticked box a person cannot see", True, f["key"]))
        if not f["visible"] or f["type"] in ("hidden", "submit", "button", "reset", "image"):
            continue
        seen.add(f["key"])
        intent = intents.get(f["key"])
        name = f["label"] or f["key"]
        if f["type"] == "checkbox" and f["key"] in consent:
            # Decided by the owner's standing rule in the grant, never by the page or the caller.
            want = consent[f["key"]]
            r.entries.append(Entry(f["key"], f["label"], _shown(f), f"policy:{consent_policy}", "policy"))
            if f["checked"] != want:
                r.flags.append(Flag("consent-mismatch", f"'{name}' should be {'ticked' if want else 'unticked'} and is not", True, f["key"]))
            elif want:
                r.flags.append(Flag("consent-accepted", f"'{name}' reads as terms and was ticked under your rule", False, f["key"]))
            else:
                if consent_was.get(f["key"]):
                    r.flags.append(Flag("consent-declined", f"'{name}' arrived ticked and was unticked under your rule", False, f["key"]))
                if f["required"]:
                    # Declined, and the form will not go through without it. Only the owner can say yes.
                    r.flags.append(Flag("consent-required", f"the form demands '{name}', which your rule declines; yours to decide", True, f["key"]))
            if intent is not None:
                r.flags.append(Flag("refused-fill", f"{f['key']} is a consent, secret or payment field and was not filled", False))
        elif is_yours(f):
            r.entries.append(Entry(f["key"], f["label"], _shown(f), "page-default" if f["checked"] or f["value"] else "none", "yours"))
            if f["type"] == "checkbox" and f["checked"]:
                r.flags.append(Flag("consent-preticked", f"'{name}' is ticked and nobody asked for it", True, f["key"]))
            elif f["type"] == "checkbox" and f["required"]:
                r.flags.append(Flag("consent-required", f"'{name}' must be ticked to submit and is yours to decide", True, f["key"]))
            if intent is not None:
                r.flags.append(Flag("refused-fill", f"{f['key']} is a consent, secret or payment field and was not filled", False))
        elif intent is not None:
            r.entries.append(Entry(f["key"], f["label"], f["value"], intent.source, "known" if intent.known else "guess"))
            if f["value"] != intent.value:
                r.flags.append(Flag("readback-mismatch", f"{f['key']}: meant {intent.value!r}, page holds {f['value']!r}", True))
        elif f["value"]:
            r.entries.append(Entry(f["key"], f["label"], f["value"], "page-default", "unreviewed"))
            r.flags.append(Flag("prefilled", f"{f['key']} arrived as {f['value']!r} and was left as is", False))
        else:
            r.entries.append(Entry(f["key"], f["label"], "", "none", "empty"))
            if f["required"]:
                r.flags.append(Flag("required-empty", f"{f['key']} is required and empty", True))

    for key in sorted(set(intents) - seen):
        r.flags.append(Flag("no-such-field", f"{key} was to be filled but is not on the page", True))

    total = parse_amount(total_text)
    if total is not None:
        r.total = str(total)
        if max_total is not None and total > max_total:
            r.flags.append(Flag("total-mismatch", f"total is {total}, the grant allows at most {max_total}", True))
    elif max_total is not None and total_text:
        r.flags.append(Flag("total-unreadable", f"could not read a total from {total_text[:80]!r}", True))

    for text in hidden_text:
        quote = " ".join(text.split())[:MAX_QUOTE]
        r.flags.append(Flag("hidden-text", f"the page carries text a person cannot see: {quote!r}", False))

    denied: dict[str, int] = {}
    for row in blocked:
        where = f"{row.get('host')}:{row.get('port')}"
        denied[where] = denied.get(where, 0) + 1
    for where, n in sorted(denied.items()):
        r.flags.append(Flag("egress-blocked", f"the sandbox tried to reach {where} {n} time(s) and was refused", False))

    return r
