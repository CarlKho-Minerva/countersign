"""The owner's standing rule for consent boxes: accept terms, decline marketing and
anything optional, and leave every other required statement to the owner.

A box is judged by what it says, not by whether the form marks it required: a form can
mark a data-sharing box required, and a required box can be a statement of fact ("I am
authorised to work in...") that is not consent at all. Wording is matched with plain
patterns here. They are deliberately narrow: when in doubt a box is not ticked.
"""

from __future__ import annotations

import re

_MARKETING = re.compile(
    r"\b(marketing|newsletters?|promot\w*|offers?|deals|advertis\w*|sponsors?|partners?|third[- ]part\w*|affiliates?"
    r"|share (my|your)|sell (my|your)|subscribe|mailing list|product (news|updates?)|updates? (about|from|on)"
    r"|keep me (posted|updated|informed)|contact me|sms|text messages?|surveys?|research panel)\b",
    re.I,
)
_TERMS = re.compile(
    r"\b(agree|accept|consent|acknowledge)\b.{0,160}?"
    r"\b(terms|conditions|polic(y|ies)|code of conduct|rules|agreement|waiver|privacy|t&cs?|tos|eula|guidelines)\b",
    re.I | re.S,
)


def classify(label: str, key: str = "") -> str:
    """'marketing', 'terms' or 'other'. Marketing wins when a box bundles both."""
    text = f"{label} {key.replace('_', ' ').replace('-', ' ')}"
    if _MARKETING.search(text):
        return "marketing"
    if _TERMS.search(label):
        return "terms"
    return "other"


def decide(field: dict) -> bool | None:
    """True: tick. False: untick. None: not the rule's to decide, leave it to the owner."""
    kind = classify(field.get("label") or "", field.get("key") or "")
    if kind == "terms":
        return True
    if kind == "marketing":
        return False  # even if the form demands it; the receipt then says the form cannot be submitted as is
    return None if field.get("required") else False
