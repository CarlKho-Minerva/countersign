"""The control plane behind the web app and the agent API: storage, the owner's signing
key, a small job queue, and the four operations (learn, fill, call, approve).

Everything a run produces is written to disk as it happens, so a page reload, a second
browser or another agent sees the same state. Every job ends in "done", "failed" or
"refused" with its reason; none ends quietly.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import run
from ..grant import CONSENT_POLICIES, Grant, GrantError, issue, verify
from ..mapper import map_facts
from ..model import Model
from ..policy import EgressPolicy
from ..receipt import Intent
from ..sandbox import Limits, Sandbox, SandboxError, reap
from ..tool import Tool, ToolCallError, compile_tool
from ..witness import check_confirmation, check_fill

MAX_QUEUE = 8
FORM_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


class Busy(Exception):
    """Too many runs waiting. Try again shortly."""


class Forbidden(Exception):
    """Not allowed for this caller."""


class NotFound(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(value, indent=1, default=str))
    os.replace(tmp, path)


def _load_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return key


def default_sandbox(private_ok: frozenset[str], wall_seconds: int):
    def factory(policy: EgressPolicy) -> Sandbox:
        # Names allowed to resolve to private addresses: only the bundled fixture site, by name.
        return Sandbox(replace(policy, private_ok=frozenset(policy.allow_hosts) & private_ok),
                       limits=Limits(wall_seconds=wall_seconds))
    return factory


class Service:
    def __init__(self, data: Path | str, *, model: Model | None, sandbox=None, demo: bool = True,
                 demo_origins: frozenset[str] = frozenset({"form.test"}), max_jobs: int = 2,
                 fixture_container: str | None = None, profile: dict | None = None):
        self.data = Path(data)
        self.model, self.demo, self.demo_origins = model, demo, demo_origins
        self.sandbox = sandbox or default_sandbox(demo_origins if demo else frozenset(), 60)
        self.fixture_container = fixture_container
        self.priv = _load_key(self.data / "keys" / "owner.pem")
        self.pub = self.priv.public_key()
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="cs-run")
        self._pending = 0
        if profile is not None and not (self.data / "profile.json").exists():
            _write_json(self.data / "profile.json", profile)

    # storage

    def _runs_dir(self) -> Path:
        return self.data / "runs"

    def profile(self) -> dict:
        p = self.data / "profile.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_profile(self, facts: dict) -> dict:
        clean = {str(k).strip(): str(v) for k, v in facts.items() if str(k).strip()}
        if len(clean) > 100 or any(len(k) > 60 or len(v) > 2000 for k, v in clean.items()):
            raise ValueError("at most 100 facts, keys up to 60 characters, values up to 2000")
        _write_json(self.data / "profile.json", clean)
        return clean

    def tools(self) -> list[Tool]:
        out = []
        for p in sorted((self.data / "tools").glob("*.json")):
            out.append(Tool.from_dict(json.loads(p.read_text())))
        return out

    def tool(self, form_id: str) -> Tool:
        p = self.data / "tools" / f"{form_id}.json"
        if not p.exists() or set(form_id) - FORM_ID_OK:
            raise NotFound(f"no form {form_id!r} has been learned")
        return Tool.from_dict(json.loads(p.read_text()))

    def grants(self) -> list[dict]:
        p = self.data / "grants.json"
        return json.loads(p.read_text()) if p.exists() else []

    def grant_record(self, grant_id: str) -> dict:
        for g in self.grants():
            if g["grant_id"] == grant_id:
                return g
        raise NotFound(f"no grant {grant_id!r}")

    def run_record(self, run_id: str) -> dict:
        p = self._runs_dir() / run_id / "run.json"
        if set(run_id) - FORM_ID_OK or not p.exists():
            raise NotFound(f"no run {run_id!r}")
        return json.loads(p.read_text())

    def runs(self, limit: int = 40) -> list[dict]:
        rows = []
        newest = sorted(self._runs_dir().glob("*/run.json"), key=lambda p: p.stat().st_mtime)[-limit:]
        for p in newest:
            try:
                rows.append(json.loads(p.read_text()))
            except ValueError:
                continue
        rows.sort(key=lambda r: r.get("created", ""), reverse=True)
        return rows[:limit]

    def _save(self, rec: dict) -> None:
        with self._lock:
            _write_json(self._runs_dir() / rec["id"] / "run.json", rec)

    def _event(self, rec: dict, msg: str) -> None:
        rec["events"].append({"t": round(time.time(), 2), "msg": msg})
        self._save(rec)

    def shot_path(self, run_id: str, n: int) -> Path:
        self.run_record(run_id)
        return self._runs_dir() / run_id / f"{int(n)}.png"

    def tool_shot_path(self, form_id: str, n: int) -> Path:
        self.tool(form_id)
        return self.data / "tools" / form_id / f"step-{int(n)}.png"

    # jobs

    def _new(self, kind: str, **fields) -> dict:
        rec = {"id": secrets.token_hex(5), "kind": kind, "status": "queued", "created": _now(), "events": [],
               "error": None, **fields}
        self._save(rec)
        return rec

    def _start(self, rec: dict, work) -> dict:
        with self._lock:
            if self._pending >= MAX_QUEUE:
                rec.update(status="refused", error="too many runs waiting; try again in a minute")
                self._save(rec)
                raise Busy(rec["error"])
            self._pending += 1

        def job():
            try:
                rec["status"] = "running"
                self._event(rec, "started")
                work(rec)
                if rec["status"] == "running":
                    rec["status"] = "done"
            except (run.OutOfScope, run.NotApproved, ToolCallError, GrantError, Forbidden) as exc:
                rec.update(status="refused", error=str(exc))
            except run.SubmitUncertain as exc:
                rec.update(status="uncertain", error=str(exc))
            except Exception as exc:  # anything else is a failure, said out loud
                rec.update(status="failed", error=f"{exc.__class__.__name__}: {exc}")
                print(json.dumps({"run": rec["id"], "failed": rec["error"], "trace": traceback.format_exc()[-1500:]}), flush=True)
            finally:
                with self._lock:
                    self._pending -= 1
                rec["finished"] = _now()
                self._event(rec, f"{rec['status']}" + (f": {rec['error']}" if rec.get("error") else ""))

        self._pool.submit(job)
        return rec

    def wait(self, run_id: str, timeout: float = 150) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rec = self.run_record(run_id)
            if rec["status"] not in ("queued", "running"):
                return rec
            time.sleep(0.25)
        return self.run_record(run_id)

    def _store_shots(self, rec: dict, trail) -> list[dict]:
        out = []
        base = len(rec.get("trail") or [])
        for i, (caption, png) in enumerate(trail):
            n = base + i
            (self._runs_dir() / rec["id"]).mkdir(parents=True, exist_ok=True)
            (self._runs_dir() / rec["id"] / f"{n}.png").write_bytes(png)
            out.append({"n": n, "caption": caption})
        return out

    # learn

    def learn(self, url: str, form_id: str | None = None) -> dict:
        origin = run.origin_of(url)
        if self.demo and origin not in self.demo_origins:
            raise Forbidden(f"this demo only learns its bundled forms on {', '.join(sorted(self.demo_origins))}")
        if not url.startswith(("http://", "https://")):
            raise ValueError("the address must start with http:// or https://")
        form_id = (form_id or url.rstrip("/").rsplit("/", 1)[-1].split(".")[0] or "form").lower()
        form_id = "".join(c if c in FORM_ID_OK else "-" for c in form_id)[:40]
        rec = self._new("learn", form_id=form_id, url=url)

        def work(rec):
            learned = run.learn_walk(form_id, url, sandbox=self.sandbox, progress=lambda m: self._event(rec, m))
            tool = compile_tool(learned, model=self.model, progress=lambda m: self._event(rec, m))
            folder = self.data / "tools" / form_id
            folder.mkdir(parents=True, exist_ok=True)
            for i, (_, png) in enumerate(learned.trail):
                (folder / f"step-{i}.png").write_bytes(png)
            _write_json(self.data / "tools" / f"{form_id}.json", tool.to_dict())
            rec["tool"] = {"form_id": form_id, "title": tool.title, "params": len(tool.params), "boxes": len(tool.boxes)}
            self._event(rec, f"learned {tool.title!r} as tool {tool.name}")

        return self._start(rec, work)

    # grants

    def issue_grant(self, *, caller: str, form_id: str, facts: list[str], max_total: str | None, may_submit: bool,
                    consent: str, ttl_minutes: int) -> dict:
        tool = self.tool(form_id)
        if consent not in CONSENT_POLICIES:
            raise ValueError(f"consent must be one of {CONSENT_POLICIES}")
        try:
            limit = None if max_total in (None, "") else Decimal(str(max_total))
        except InvalidOperation as exc:
            raise ValueError("max_total must be a number") from exc
        if not 1 <= int(ttl_minutes) <= 7 * 24 * 60:
            raise ValueError("a grant lasts between 1 minute and 7 days")
        caller = " ".join(str(caller).split())[:80] or "owner"
        g = Grant(grant_id="g-" + secrets.token_hex(4), caller=caller, form_id=form_id, origins=(tool.compiled.origin,),
                  facts=tuple(sorted({str(f) for f in facts})), max_total=limit, may_submit=bool(may_submit),
                  expires_at=int(time.time()) + int(ttl_minutes) * 60, consent=consent)
        token = issue(self.priv, g)
        rec = {**asdict(g), "max_total": None if limit is None else str(limit), "token": token, "issued": _now()}
        with self._lock:
            _write_json(self.data / "grants.json", self.grants() + [rec])
        return rec

    # fill: the owner, through the web app, with their profile and the model's mapping

    def fill_for_owner(self, form_id: str, grant_id: str) -> dict:
        tool = self.tool(form_id)
        g = self.grant_record(grant_id)
        grant = verify(self.pub, g["token"])  # expired or forged: refused here, before any job
        profile = self.profile()
        rec = self._new("fill", form_id=form_id, caller=grant.caller, grant_id=grant.grant_id, via="web")

        def work(rec):
            mapping = map_facts(tool, profile, allowed=set(grant.facts), model=self.model,
                                progress=lambda m: self._event(rec, m))
            rec["mapping"] = {"by": mapping.by, "notes": mapping.notes, "withheld": mapping.withheld,
                              "intents": {k: asdict(i) for k, i in mapping.intents.items()}}
            self._fill(rec, tool, profile, g["token"], mapping.intents)

        return self._start(rec, work)

    # call: another agent, with a grant and the tool's typed arguments

    def call_tool(self, form_id: str, args: dict, token: str) -> dict:
        tool = self.tool(form_id)
        try:
            grant = verify(self.pub, token)
        except GrantError as exc:
            raise Forbidden(str(exc)) from exc
        intents = tool.intents(args)  # ToolCallError: bad arguments, no job
        rec = self._new("fill", form_id=form_id, caller=grant.caller, grant_id=grant.grant_id, via="agent",
                        mapping={"by": "the caller's typed arguments", "notes": [], "withheld": [],
                                 "intents": {k: asdict(i) for k, i in intents.items()}})
        try:
            run._admit(tool.compiled, args, token, self.pub, intents, None, submit=False)
        except run.OutOfScope as exc:
            rec.update(status="refused", error=f"refused before any container started: {exc}", finished=_now())
            self._event(rec, rec["error"])
            raise
        return self._start(rec, lambda rec: self._fill(rec, tool, args, token, intents))

    def _fill(self, rec: dict, tool: Tool, facts: dict, token: str, intents: dict[str, Intent]) -> None:
        rec["token"] = token
        try:
            result = run.fill(tool.compiled, facts, token, self.pub, sandbox=self.sandbox, intents=intents,
                              progress=lambda m: self._event(rec, m))
        except run.OutOfScope as exc:
            raise run.OutOfScope([f"refused before any container started: {r}" for r in exc.refusals]) from None
        receipt = result.receipt
        if self.model is not None and result.trail:
            hidden = [f.detail for f in receipt.flags if f.kind == "hidden-text"]
            receipt.flags += check_fill(self.model, receipt, [png for _, png in result.trail], hidden,
                                        progress=lambda m: self._event(rec, m))
        rec["trail"] = self._store_shots(rec, result.trail)
        rec["receipt"] = receipt.to_dict()
        rec["proxy_log"] = result.proxy_log[-200:]
        rec["status"] = "done"
        self._event(rec, "receipt ready: " + ("approvable" if receipt.approvable else "blocked until you decide what is flagged"))

    # approve: only the owner, only the receipt they read

    def approve(self, run_id: str, digest: str, consent: dict[str, bool]) -> dict:
        with self._lock:  # check and claim in one step: two clicks must never mean two submissions
            fill = self.run_record(run_id)
            if fill.get("kind") != "fill" or fill.get("status") != "done" or not fill.get("receipt"):
                raise Forbidden("only a finished fill can be approved")
            if fill["receipt"]["digest"] != digest:
                raise Forbidden("this is not the receipt you were shown; reload it")
            if fill.get("approved_by"):
                raise Forbidden(f"already approved; see run {fill['approved_by']}")
            if fill.get("rejected"):
                raise Forbidden("this receipt was rejected")
            tool = self.tool(fill["form_id"])
            intents = {k: Intent(v["value"], v["source"], tuple(v.get("uses") or ())) for k, v in fill["mapping"]["intents"].items()}
            decisions = {str(k): bool(v) for k, v in (consent or {}).items()}
            rec = self._new("submit", form_id=fill["form_id"], caller=fill["caller"], grant_id=fill["grant_id"], of=run_id,
                            consent=decisions)
            fill["approved_by"] = rec["id"]
            self._save(fill)

        def work(rec):
            result = run.submit(tool.compiled, {}, fill["token"], self.pub, run.Approval(digest, decisions),
                                sandbox=self.sandbox, intents=intents, progress=lambda m: self._event(rec, m))
            rec["trail"] = self._store_shots(rec, result.trail)
            rec["landed"] = result.landed
            rec["receipt"] = result.receipt.to_dict()
            rec["proxy_log"] = result.proxy_log[-200:]
            if self.model is not None and result.confirmation_png:
                self._event(rec, f"asking {self.model.cfg.label} to read the page Submit led to")
                rec["confirmation"] = asdict(check_confirmation(self.model, result.confirmation_png, result.landed))
            self._event(rec, f"submitted; the site answered with {result.landed.get('url')}")

        try:
            return self._start(rec, work)
        except Busy:
            fill.pop("approved_by", None)
            self._save(fill)
            raise

    def reject(self, run_id: str) -> dict:
        with self._lock:
            fill = self.run_record(run_id)
            if fill.get("approved_by"):
                raise Forbidden("already approved")
            fill["rejected"] = _now()
            self._event(fill, "rejected by the owner; nothing was submitted")
        return fill

    # evidence for the demo: what the fixture site actually received

    def fixture_log(self) -> dict:
        if not self.fixture_container:
            return {"available": False}
        cached = getattr(self, "_fixture_cache", None)
        if cached and time.monotonic() - cached[0] < 2.5:  # every open page polls this; docker logs is not free
            return cached[1]
        value = self._fixture_log()
        self._fixture_cache = (time.monotonic(), value)
        return value

    def _fixture_log(self) -> dict:
        import subprocess
        try:
            out = subprocess.run(["docker", "logs", "--tail", "2000", self.fixture_container], capture_output=True,
                                 text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "error": str(exc)}
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[:300]}
        rows = []
        for line in out.stdout.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rows.append({"host": r.get("host"), "method": r.get("method"), "path": (r.get("path") or "")[:80],
                         "bytes": len(r.get("body") or "")})
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["host"]] = counts.get(r["host"], 0) + 1
        return {"available": True, "counts": counts, "recent": rows[-15:],
                "collector": counts.get("collector.test", 0),
                "posts": sum(1 for r in rows if r["method"] == "POST" and r["host"] == "form.test")}

    def prune(self, keep: int = 300) -> list[str]:
        """Remove all but the newest `keep` finished runs: a public demo must not fill the disk."""
        done = [p.parent for p in sorted(self._runs_dir().glob("*/run.json"), key=lambda p: p.stat().st_mtime)]
        gone = []
        for folder in done[:-keep] if len(done) > keep else []:
            try:
                if json.loads((folder / "run.json").read_text()).get("status") in ("queued", "running"):
                    continue
            except ValueError:
                pass
            shutil.rmtree(folder, ignore_errors=True)
            gone.append(folder.name)
        return gone

    def reap_forever(self, every: int = 300) -> None:
        def loop():
            while True:
                try:
                    gone = reap(max_age_seconds=600)
                    if gone:
                        print(json.dumps({"reaped": gone}), flush=True)
                except SandboxError as exc:
                    print(json.dumps({"reap_failed": str(exc)}), flush=True)
                pruned = self.prune()
                if pruned:
                    print(json.dumps({"pruned_runs": len(pruned)}), flush=True)
                time.sleep(every)
        threading.Thread(target=loop, daemon=True, name="cs-reaper").start()


def png_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()
