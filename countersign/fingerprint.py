"""A form's shape, and how a live page differs from the compiled one.

Shape is what a replay depends on: which controls exist, their kind, whether they are
required, and the choices they offer. Values, labels and option order are not shape, so
a rotating CSRF token or a reworded label does not count as drift.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field


def _shape(fields: list[dict]) -> list[tuple]:
    rows = [
        (f["key"], f["tag"], f["type"], bool(f["required"]), tuple(sorted(f.get("options") or [])))
        for f in fields
    ]
    return sorted(rows)


def fingerprint(fields: list[dict]) -> str:
    canon = json.dumps(_shape(fields), separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


@dataclass
class Drift:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def lines(self) -> list[str]:
        out = [f"added: {k}" for k in self.added] + [f"removed: {k}" for k in self.removed]
        out += [f"{k}: {', '.join(v)}" for k, v in self.changed.items()]
        return out


def drift(compiled: list[dict], live: list[dict]) -> Drift:
    old_n, new_n = Counter(f["key"] for f in compiled), Counter(f["key"] for f in live)
    old = {f["key"]: f for f in compiled}
    new = {f["key"]: f for f in live}
    d = Drift(
        added=sorted(k for k in new if k not in old),
        removed=sorted(k for k in old if k not in new),
    )
    for key in sorted(old.keys() & new.keys()):
        a, b, why = old[key], new[key], []
        if old_n[key] != new_n[key]:
            why.append(f"count {old_n[key]} -> {new_n[key]}")
        for attr in ("tag", "type", "required"):
            if a[attr] != b[attr]:
                why.append(f"{attr} {a[attr]} -> {b[attr]}")
        gone = sorted(set(a.get("options") or []) - set(b.get("options") or []))
        came = sorted(set(b.get("options") or []) - set(a.get("options") or []))
        if gone:
            why.append("options removed: " + ", ".join(gone))
        if came:
            why.append("options added: " + ", ".join(came))
        if why:
            d.changed[key] = why
    return d
