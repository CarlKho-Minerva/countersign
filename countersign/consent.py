"""The owner's standing rule for consent boxes: accept terms, decline marketing and
anything optional, and leave every other required statement to the owner.

A box is judged by what it says, not by whether the form marks it required: a form can
mark a data-sharing box required, and a required box can be a statement of fact ("I am
authorised to work in...") that is not consent at all. Wording is matched with plain
patterns here. They are deliberately narrow: when in doubt a box is not ticked.

The model may read a label when the form is learned (`tool.py`). Its reading can only
make the rule more careful, with one exception: it may call an oddly worded box terms
("Yes, I've read the house rules and I'm in"), and only if the label names terms,
rules, a policy or the like. Even then the patterns below run first, on the label as
the live page shows it, and a statement, an authorisation or anything that reads as
marketing is never ticked, whatever the model said.
"""

from __future__ import annotations

import re

KINDS = ("terms", "marketing", "owner", "optional")

_MARKETING = re.compile(
    r"\b(marketing|newsletters?|news|promot\w*|offers?|deals|advertis\w*|sponsors?|partners?|third[- ]part\w*|affiliates?"
    r"|share (my|your)|sell (my|your)|subscribe|mailing list|product (news|updates?)|updates? (about|from|on)"
    r"|keep me (posted|updated|informed)|contact me|sms|text messages?|surveys?|research panel)\b",
    re.I,
)
_TERMS = re.compile(
    r"\b(agree|accept|consent|acknowledge)\b.{0,160}?"
    r"\b(terms|conditions|polic(y|ies)|code of conduct|rules|agreement|waiver|privacy|t&cs?|tos|eula|guidelines)\b",
    re.I | re.S,
)
# What a terms box is about. The model may call an oddly worded box terms only if it names one of these.
_TERMS_NOUN = re.compile(r"\b(terms|conditions|polic(y|ies)|code of conduct|rules|agreement|waiver|privacy|t&cs?|tos|eula|guidelines)\b", re.I)
# Statements of fact and authorisations. Only the owner can make these, so no rule and
# no model ever ticks one.
_OWNER = re.compile(
    r"\b(authori[sz]\w*|eligible|eligibility|citizen\w*|permanent resident|resident of|visa|sponsorship|immigration"
    r"|work permit|right to work|over (the age of )?\d+|under \d+|\d+ (years|yrs) (of age|old)|age of|years of age"
    r"|certify|certification|attest|declare|swear|affirm|true and (correct|accurate)|accurate|truthful|penalty of perjury"
    r"|criminal|convict\w*|felony|disabilit\w*|veteran|gender|race|ethnicity"
    r"|charge[sd]?|billing|bill me|payments?|pay|debit|recurring|auto-?renew\w*|subscription|purchase)\b",
    re.I,
)


def classify(label: str, key: str = "") -> str:
    """'marketing', 'terms' or 'other'. Marketing wins when a box bundles both."""
    text = f"{label} {key.replace('_', ' ').replace('-', ' ')}"
    if _MARKETING.search(text):
        return "marketing"
    if _TERMS.search(label):
        return "terms"
    return "other"


def owner_only(label: str, key: str = "") -> bool:
    """A statement of fact or an authorisation: never the rule's or the model's to make."""
    return bool(_OWNER.search(f"{label} {key.replace('_', ' ').replace('-', ' ')}"))


def kind(label: str, key: str = "", model_kind: str | None = None) -> str:
    """The box's kind under the rule, with the model's reading folded in carefully."""
    rule = classify(label, key)
    if rule == "marketing" or model_kind == "marketing":
        return "marketing"
    if owner_only(label, key) or model_kind == "owner":
        return "owner"
    if rule == "terms" or (model_kind == "terms" and _TERMS_NOUN.search(label)):
        return "terms"
    return "optional" if model_kind == "optional" else "other"


def decide(field: dict, model_kind: str | None = None) -> bool | None:
    """True: tick. False: untick. None: not the rule's to decide, leave it to the owner."""
    k = kind(field.get("label") or "", field.get("key") or "", model_kind)
    if k == "terms":
        return True
    if k == "marketing":
        return False  # even if the form demands it; the receipt then says the form cannot be submitted as is
    return None if field.get("required") else False
