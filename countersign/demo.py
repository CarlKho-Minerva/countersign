"""The end-to-end demo against the bundled test forms, with the real sandbox and a real
model. Every claim it prints is checked; any check that fails makes it exit 1.

    python -m countersign.demo [--out runs/demo-...] [--no-model]

Needs a Docker engine with both images built (sandbox/build.sh). It starts the test
site itself (form.test and collector.test on a private Docker network) and removes it
afterwards. The model is whatever CS_MODEL_PROVIDER names (see .env.example); with
--no-model the same flow runs on rules alone.

What it shows, in order:
 1. learn a form in two steps and compile it into a typed tool
 2. map a made-up person's facts to it, each value known or guessed; the passport
    number the grant leaves out is never sent to the model
 3. fill it in a sandbox, a screenshot per step, and have the model check them
 4. countersign: a second sandbox replays the fill, matches the receipt, submits;
    the model reads the confirmation page
 5. a hostile page: hidden instructions, a script that tries to send what is typed to
    another host, a $1,278 total. The proxy refuses, the receipt blocks, and the
    collector receives nothing
 6. an agent holding a grant for one form asks for another: refused before a
    container exists
 7. a form that asks for a work-authorisation statement: never ticked; it stays the
    owner's, and Submit is refused until they decide
 8. a form that changed shape since it was learned: not filled
 9. a page that never stops: the time cap destroys the sandbox
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

from . import run
from .grant import Grant, issue, new_keypair
from .mapper import map_facts
from .model import Model
from .sandbox import Images, Limits, Sandbox, SandboxKilled, ensure_egress_network
from .tool import compile_tool
from .witness import check_confirmation, check_fill

REPO = Path(__file__).resolve().parent.parent
FORM = "http://form.test"
PROFILE = {  # a made-up person; nothing here is anyone's real data
    "full_name": "Ada Example", "email": "ada@example.org", "country": "Philippines", "role": "Founder, Example Labs",
    "session_pref": "mornings", "diet": "Vegetarian", "passport_no": "X0000000",
}
ALLOWED = ("full_name", "email", "country", "role", "session_pref", "diet")
NET = "cs-egress-demo"  # its own network: a deployed stack's test site on cs-egress must not answer for form.test


class Demo:
    def __init__(self, out: Path, model: Model | None):
        self.out, self.model = out, model
        self.priv, self.pub = new_keypair()
        self.checks: list[tuple[str, bool]] = []
        self.lines: list[str] = []
        self.site = f"cs-demo-site-{secrets.token_hex(3)}"

    # reporting

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def check(self, claim: str, ok: bool) -> bool:
        self.checks.append((claim, bool(ok)))
        self.say(f"  [{'PASS' if ok else 'FAIL'}] {claim}")
        return ok

    def shot(self, name: str, png: bytes | None) -> str:
        if not png:
            return ""
        p = self.out / f"{name}.png"
        p.write_bytes(png)
        self.lines.append(f"![{name}]({p.name})")
        return p.name

    # plumbing

    def box(self, wall: int = 60):
        return lambda p: Sandbox(replace(p, private_ok=frozenset(p.allow_hosts) & {"form.test"}), limits=Limits(wall_seconds=wall),
                                 images=Images(egress_network=NET))

    def token(self, **over) -> str:
        base = dict(grant_id="g-" + secrets.token_hex(3), caller="you, in this app", form_id="wizard", origins=("form.test",),
                    facts=ALLOWED, max_total=Decimal("0"), may_submit=True, expires_at=int(time.time()) + 900,
                    consent="accept-terms")
        base.update(over)
        return issue(self.priv, Grant(**base))

    def start_site(self) -> None:
        ensure_egress_network(NET)
        subprocess.run(["docker", "run", "-d", "--name", self.site, "--network", NET, "--network-alias", "form.test",
                        "--network-alias", "collector.test", "-v", f"{REPO / 'fixtures'}:/srv:ro", "python:3.13-slim",
                        "python3", "/srv/serve.py", "80"], check=True, capture_output=True, text=True)
        time.sleep(1.0)

    def stop_site(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.site], capture_output=True)

    def hits(self, host: str, method: str | None = None) -> list[dict]:
        out = subprocess.run(["docker", "logs", self.site], capture_output=True, text=True).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        return [r for r in rows if r["host"] == host and (method is None or r["method"] == method)]

    def receipt_table(self, r) -> None:
        for e in r.entries:
            self.say(f"    {e.key:<16} {e.value[:40]!r:<44} {e.status:<10} {e.source}")
        for f in r.flags:
            self.say(f"    {'BLOCK' if f.blocking else 'note '} {f.kind}: {f.detail[:150]}")

    # the scenes

    def learn(self, form_id: str, page: str):
        learned = run.learn_walk(form_id, f"{FORM}/{page}", sandbox=self.box())
        return learned, compile_tool(learned, model=self.model)

    def scene_happy_path(self) -> None:
        self.say("\n## 1. Learn a form once, compile it into a typed tool")
        learned, tool = self.learn("wizard", "wizard.html")
        for i, (_, png) in enumerate(learned.trail):
            self.shot(f"1-learn-step-{i + 1}", png)
        self.say(f"  tool {tool.name}: {tool.title!r}, read by {tool.learned_by}")
        self.say(f"  purpose: {tool.purpose}")
        self.say("  parameters: " + ", ".join(f"{p.key}:{p.kind}{'*' if p.required else ''}" for p in tool.params))
        self.say("  consent boxes: " + ", ".join(f"{b.key} -> {b.kind}" for b in tool.boxes))
        for n in tool.notes:
            self.say(f"  note: {n}")
        (self.out / "tool-wizard.json").write_text(json.dumps(tool.definition(), indent=1))
        self.check("the form's two steps were walked without pressing Submit", tool.compiled.steps == ("next_1",))
        self.check("the tool's choices are the page's own", tool.input_schema()["properties"]["country"]["enum"] == ["US", "PH", "JP", "DE"])
        news = {b.key: b.kind for b in tool.boxes}
        self.check("the newsletter box reads as marketing", news.get("workshop_news") == "marketing")
        if self.model:
            self.check("the model read the oddly worded rules box as terms", news.get("house_rules") == "terms")

        self.say("\n## 2. Map the owner's facts, known or guessed")
        grant = self.token()
        mapping = map_facts(tool, PROFILE, allowed=ALLOWED, model=self.model)
        for k, i in mapping.intents.items():
            self.say(f"    {k:<14} {i.value!r:<24} {'known' if i.known else 'guess'}  {i.source}")
        for n in mapping.notes:
            self.say(f"    note: {n}")
        self.check("the passport number was withheld from the model", mapping.withheld == ["passport_no"])
        self.check("the name and email are known facts", all(mapping.intents.get(k) and mapping.intents[k].known for k in ("full_name", "email")))
        if self.model:
            c = mapping.intents.get("country")
            self.check("'Philippines' became the form's option PH, still a known fact", bool(c) and c.value == "PH" and c.known)
            guesses = [k for k, i in mapping.intents.items() if not i.known]
            self.check("at least one value is labelled a guess", bool(guesses))

        self.say("\n## 3. Fill in a sandbox, stop before Submit, check the screenshots")
        res = run.fill(tool.compiled, PROFILE, grant, self.pub, sandbox=self.box(), intents=mapping.intents)
        r = res.receipt
        for i, (cap, png) in enumerate(res.trail):
            self.shot(f"3-fill-{i + 1}", png)
        if self.model:
            hidden = [f.detail for f in r.flags if f.kind == "hidden-text"]
            r.flags += check_fill(self.model, r, [p for _, p in res.trail], hidden)
        self.receipt_table(r)
        self.check("one screenshot per step", len(res.trail) == 2)
        self.check("nothing was submitted by the fill", not self.hits("form.test", "POST"))
        by = {e.key: e for e in r.entries}
        self.check("the pre-ticked newsletter was unticked under the owner's rule", by["workshop_news"].value == "not ticked")
        if self.model:
            self.check("the receipt is approvable", r.approvable)
        else:
            self.check("without a model the rules box is left to the owner", not r.approvable)
            return

        self.say("\n## 4. Countersign: a second sandbox replays, matches, submits")
        done = run.submit(tool.compiled, PROFILE, grant, self.pub, run.Approval(r.digest()), sandbox=self.box(),
                          intents=mapping.intents)
        self.shot("4-before-submit", done.screenshot_png)
        self.shot("4-after-submit", done.confirmation_png)
        posts = self.hits("form.test", "POST")
        self.check("exactly one submission reached the site", len(posts) == 1)
        body = posts[0]["body"] if posts else ""
        self.check("it carries the approved values and not the declined newsletter",
                   "full_name=Ada+Example" in body and "workshop_news" not in body and "X0000000" not in body)
        if self.model:
            conf = check_confirmation(self.model, done.confirmation_png, done.landed)
            self.say(f"  model reads the confirmation: {conf.detail}")
            self.check("the model reads the page as accepted", conf.detail.startswith("accepted"))

    def scene_hostile(self) -> None:
        self.say("\n## 5. A hostile page")
        _, tool = self.learn("hostile", "hostile.html")
        grant = self.token(form_id="hostile", facts=ALLOWED)
        mapping = map_facts(tool, PROFILE, allowed=ALLOWED, model=self.model)
        res = run.fill(tool.compiled, PROFILE, grant, self.pub, sandbox=self.box(), intents=mapping.intents)
        r = res.receipt
        if self.model:
            r.flags += check_fill(self.model, r, [p for _, p in res.trail], [f.detail for f in r.flags if f.kind == "hidden-text"])
        self.shot("5-hostile", res.screenshot_png)
        self.receipt_table(r)
        denied = {(x["host"], x["port"]) for x in res.proxy_log if x.get("action") == "deny"}
        self.check("the collector never received anything", self.hits("collector.test") == [])
        self.check("the proxy refused the collector, the metadata address and the open internet",
                   {("collector.test", 80), ("169.254.169.254", 80), ("example.com", 443)} <= denied)
        self.check("the $1,278 total blocks approval", any(f.kind == "total-mismatch" and f.blocking for f in r.flags))
        self.check("the hidden instructions are quoted, not followed",
                   any("tick" in f.detail for f in r.flags if f.kind == "hidden-text")
                   and {e.key: e.value for e in r.entries}["share_with_sponsors"] == "not ticked")
        self.check("the passport number is nowhere in the receipt", "X0000000" not in json.dumps(r.to_dict()))
        if self.model:
            self.check("the model witness flags the text aimed at it", any(f.kind == "model-injection" for f in r.flags))

    def scene_scope(self) -> None:
        self.say("\n## 6. An agent with a grant for one form asks for another")
        _, tool = self.learn("plain", "plain.html")
        started = []
        agent = self.token(caller="agent:travel-desk", form_id="wizard", facts=("full_name", "email"))
        try:
            run.fill(tool.compiled, {"full_name": "Ada Example", "email": "ada@example.org"}, agent, self.pub,
                     sandbox=lambda p: started.append(p))
            refused = ""
        except run.OutOfScope as exc:
            refused = str(exc)
        self.say(f"  refused: {refused}")
        self.check("refused at the boundary, no container started", bool(refused) and not started)

    def scene_consent(self) -> None:
        self.say("\n## 7. A statement only the owner can make")
        _, tool = self.learn("apply", "consent.html")
        grant = self.token(form_id="apply", facts=("full_name",), max_total=None)
        res = run.fill(tool.compiled, PROFILE, grant, self.pub, sandbox=self.box(),
                       intents={"full_name": run.rc.Intent("Ada Example", "fact:full_name", ("full_name",))})
        r = res.receipt
        self.shot("7-consent", res.screenshot_png)
        self.receipt_table(r)
        by = {e.key: e for e in r.entries}
        self.check("the work-authorisation box is not ticked and is the owner's", by["work_auth"].value == "not ticked"
                   and by["work_auth"].status in ("yours", "policy"))
        try:
            run.submit(tool.compiled, PROFILE, grant, self.pub, run.Approval(r.digest()), sandbox=self.box(),
                       intents={"full_name": run.rc.Intent("Ada Example", "fact:full_name", ("full_name",))})
            refused = False
        except run.NotApproved as exc:
            refused = "work_auth" in str(exc) or "authorized" in str(exc)
        self.check("Submit is refused until the owner decides it", refused)

    def scene_drift_and_cap(self) -> None:
        self.say("\n## 8. A form that changed shape since it was learned")
        compiled = run.learn("plain", f"{FORM}/plain.html", sandbox=self.box())
        moved = replace(compiled, url=f"{FORM}/changed.html")
        r = run.fill(moved, {"full_name": "Ada Example"}, self.token(form_id="plain"), self.pub, sandbox=self.box()).receipt
        self.say(f"  {r.flags[0].kind}: {r.flags[0].detail}")
        self.check("drift is reported and nothing is filled", r.flags[0].kind == "drift" and not r.approvable)

        self.say("\n## 9. A page that never stops")
        began = time.monotonic()
        killed = ""
        try:
            with self.box(wall=8)(run.EgressPolicy(allow_hosts=frozenset({"form.test"}))) as b:
                b.call("open", url=f"{FORM}/spin.html")
        except SandboxKilled as exc:
            killed = str(exc)
        self.say(f"  {killed} after {time.monotonic() - began:.1f}s")
        self.check("the time cap destroyed the sandbox", "time cap" in killed)

    def run_all(self, scenes) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        self.say(f"# Countersign demo, {time.strftime('%Y-%m-%d %H:%M %Z')}")
        self.say(f"model: {self.model.cfg.label if self.model else 'none (rules only)'}")
        self.start_site()
        try:
            for scene in scenes:
                try:
                    scene()
                except Exception as exc:  # a scene that crashes is a failed check, never a skipped one
                    self.check(f"{scene.__name__} ran to the end ({exc.__class__.__name__}: {exc})", False)
        finally:
            self.stop_site()
        left = subprocess.run(["docker", "ps", "-aq", "--filter", "label=countersign.run"], capture_output=True, text=True).stdout.split()
        self.check("no sandbox container was left behind", not left)
        failed = [c for c, ok in self.checks if not ok]
        self.say(f"\n{len(self.checks) - len(failed)}/{len(self.checks)} checks passed")
        if self.model:
            s = self.model.session
            self.say(f"model calls: {s.calls}, tokens in {s.tokens_in}, out {s.tokens_out}, cost ${s.usd:.4f}")
        (self.out / "report.md").write_text("\n".join(self.lines) + "\n")
        (self.out / "checks.json").write_text(json.dumps({"checks": self.checks, "model": asdict(self.model.session) if self.model else None}, indent=1))
        print(f"report: {self.out / 'report.md'}")
        return 1 if failed else 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=f"runs/demo-{time.strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--no-model", action="store_true")
    args = ap.parse_args(argv)
    model = None if args.no_model else Model.from_env()
    if model is None and not args.no_model:
        sys.exit("no model configured: set CS_MODEL_PROVIDER (see .env.example) or pass --no-model")
    d = Demo(Path(args.out), model)
    sys.exit(d.run_all([d.scene_happy_path, d.scene_hostile, d.scene_scope, d.scene_consent, d.scene_drift_and_cap]))


if __name__ == "__main__":
    main()
