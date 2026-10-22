"""Drive the running web app through the whole demo, as a person would, and keep the
evidence: a screenshot at each stage and, with --video, a captioned screen recording.

    uv run --with playwright python scripts/walkthrough.py http://localhost:8090 --out dist/walkthrough [--video]

The app must be running in demo mode with a model configured. Every stage waits for the
real result (a learned tool, a receipt, a submission) and fails loudly if it does not
come; nothing here is mocked. Captions are drawn over the page; there is no voice.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

W, H = 1600, 900


class Walk:
    def __init__(self, page: Page, base: str, out: Path, video: bool):
        self.p, self.base, self.out, self.video = page, base.rstrip("/"), out, video
        self.n = 0
        self.log: list[dict] = []
        self.t0 = time.monotonic()

    def api(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return json.loads(r.read())

    def caption(self, text: str, hold: float = 3.0) -> None:
        self.log.append({"t": round(time.monotonic() - self.t0, 2), "caption": text})
        self.p.evaluate("""(text) => {
            let c = document.getElementById('cs-caption');
            if (!c) {
                c = document.createElement('div'); c.id = 'cs-caption';
                c.style.cssText = 'position:fixed;left:50%;bottom:54px;transform:translateX(-50%);max-width:1100px;' +
                  'background:rgba(28,25,23,.92);color:#fafaf9;font:600 21px/1.35 Inter,system-ui,sans-serif;' +
                  'padding:12px 22px;border-radius:10px;z-index:99;box-shadow:0 6px 30px rgba(0,0,0,.25);text-align:center';
                document.body.appendChild(c);
            }
            c.textContent = text; c.style.display = text ? 'block' : 'none';
        }""", text)
        if self.video:
            self.p.wait_for_timeout(int(hold * 1000))

    def shot(self, name: str, full: bool = False) -> None:
        self.n += 1
        if not self.video:  # stills are for slides: no caption over the content
            self.p.evaluate("() => { const c = document.getElementById('cs-caption'); if (c) c.style.display = 'none'; }")
        self.p.screenshot(path=str(self.out / f"{self.n:02d}-{name}.png"), full_page=full)

    def card(self, name: str, heading: str) -> None:
        """A still of one card, for slides. Only in stills mode."""
        if self.video:
            return
        self.p.evaluate("() => { const c = document.getElementById('cs-caption'); if (c) c.style.display = 'none'; }")
        loc = self.p.locator(".card", has=self.p.locator("h2", has_text=heading)).first if not heading.startswith("#") \
            else self.p.locator(heading).first
        loc.scroll_into_view_if_needed()
        loc.screenshot(path=str(self.out / f"card-{name}.png"))

    def show(self, selector: str, hold: float = 0.0) -> None:
        loc = self.p.locator(selector).first
        loc.scroll_into_view_if_needed()
        self.p.evaluate("(el) => el.scrollIntoView({behavior: 'smooth', block: 'center'})", loc.element_handle())
        self.p.wait_for_timeout(700)
        if hold and self.video:
            self.p.wait_for_timeout(int(hold * 1000))

    def wait_hash(self, prefix: str, timeout: float = 180) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            h = self.p.evaluate("location.hash")
            if h.startswith(prefix):
                return h
            self.p.wait_for_timeout(300)
        raise SystemExit(f"never reached {prefix} (at {self.p.evaluate('location.hash')})")

    def wait_run(self, timeout: float = 60) -> str:
        """The id of the run the page moved to: a new #run/<id>, not #run/new/... and not the one before."""
        before = self.p.evaluate("location.hash")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            h = self.p.evaluate("location.hash")
            parts = h.split("/")
            if h != before and len(parts) == 2 and parts[0] == "#run" and parts[1]:
                return parts[1]
            self.p.wait_for_timeout(200)
        raise SystemExit(f"no new run page (at {self.p.evaluate('location.hash')})")

    def wait_text(self, text: str, timeout: float = 180) -> None:
        self.p.get_by_text(text, exact=False).first.wait_for(timeout=timeout * 1000)

    def run_status(self, run_id: str, timeout: float = 180) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.api(f"/api/runs/{run_id}")
            if r["status"] not in ("queued", "running"):
                return r
            time.sleep(0.5)
        raise SystemExit(f"run {run_id} did not finish")

    # stages

    def learn(self, page_name: str, title_hint: str) -> None:
        self.p.goto(self.base + "/#learn")
        self.p.wait_for_load_state("networkidle")
        self.p.locator(f".pick[data-url$='{page_name}']").click()
        self.p.locator("#learn").click()
        self.wait_text("Learning")

    def grant(self, form_id: str, *, uncheck: tuple[str, ...] = ("passport_no",), max_total: str = "0") -> dict:
        self.p.goto(f"{self.base}/#grant/{form_id}")
        self.p.wait_for_selector("#issue")
        for box in self.p.locator(".facts input").all():
            want = box.get_attribute("value") not in uncheck
            if box.is_checked() != want:
                box.click()
        self.p.fill("#g-max", max_total)
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--out", default="dist/walkthrough")
    ap.add_argument("--video", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1 if a.video else 2,
                                  record_video_dir=str(out / "raw") if a.video else None,
                                  record_video_size={"width": W, "height": H} if a.video else None)
        page = ctx.new_page()
        w = Walk(page, a.base, out, a.video)
        try:
            script(w)
        finally:
            (out / "captions.json").write_text(json.dumps(w.log, indent=1))
            ctx.close()
            browser.close()
    print(f"walkthrough: {out}")


def script(w: Walk) -> None:
    p = w.p
    # 1. Learn
    p.goto(w.base + "/#learn")
    p.wait_for_load_state("networkidle")
    w.caption("Countersign: forms your agents can fill, in a sandbox, with a receipt you sign.", 4)
    w.caption("Step 1: learn a form once. This one has two steps, an oddly worded rules box and a pre-ticked newsletter.", 3.5)
    p.locator(".pick[data-url$='wizard.html']").click()
    p.locator("#learn").click()
    w.wait_text("Learning")
    w.caption("A browser in a throwaway sandbox walks the form, pressing Next but never Submit. Only form.test is reachable.", 1)
    w.wait_text("asking", 120)
    w.shot("learn-progress")
    w.caption("The model reads the controls and screenshots. Types and choices come from the page, never from the model.", 1)
    w.wait_hash("#tool/", 180)
    p.wait_for_selector("h1")
    p.wait_for_timeout(600)
    w.caption("The form is now a typed tool: parameters, steps, and how each consent box reads.", 3.5)
    w.shot("tool-top")
    w.show("text=Consent boxes", 0.5)
    w.caption("The model read the rules box as terms. The newsletter is marketing, so your rule leaves it unticked.", 4)
    w.shot("tool-consent")
    w.card("tool-params", "Parameters of")
    w.card("tool-consent", "Consent boxes")
    w.show("pre.json", 0.5)
    w.caption("Another agent sees this JSON Schema over MCP. No vision model is needed to replay the form.", 3.5)
    w.shot("tool-schema")

    # 2. Grant
    p.click("text=Grant access to this form")
    p.wait_for_selector("#issue")
    w.caption("Step 2: grant access. A signed, expiring grant: which facts, the largest total, may it submit, the consent rule.", 4)
    w.show(".facts", 0.3)
    w.caption("Ada Example is a made-up person. Her passport number stays out of the grant, so the model never sees it.", 4)
    w.shot("grant-form")
    p.click("#issue")
    w.wait_text("Signed grant")
    w.show("#issued", 0.3)
    w.caption("Signed with the owner's key (Ed25519). Anything outside it is refused before a container starts.", 3.5)
    w.shot("grant-issued")

    # 3. Fill
    p.click("text=Fill it now with this grant")
    p.wait_for_selector("#start")
    p.click("#start")
    run_id = w.wait_run()
    w.caption("Step 3: fill. The model maps Ada's allowed facts to fields, then a fresh sandbox fills both steps and stops before Submit.", 1)
    w.wait_text("sandbox", 60)
    w.shot("fill-progress")
    rec = w.run_status(run_id)
    if rec["status"] != "done":
        raise SystemExit(f"fill ended {rec['status']}: {rec.get('error')}")
    p.wait_for_selector("text=Receipt")
    p.wait_for_timeout(800)
    p.evaluate("window.scrollTo({top: 0})")
    w.caption("The receipt. Every field, where its value came from, and whether it is known or a guess.", 4)
    w.shot("receipt-top")
    w.show("table", 0.5)
    w.caption("'Philippines' became the option PH: still a known fact. Organisation and session are labelled guesses, with reasons.", 4.5)
    w.shot("receipt-fields")
    w.card("receipt", "Receipt")
    if p.locator("text=Model witness").count():
        w.show("text=Model witness", 0.3)
        w.caption("The model checks each screenshot against the receipt. A witness, never a gate: it cannot approve or unblock.", 4)
        w.shot("receipt-witness")
        w.card("witness", "Model witness")
    w.show("text=Screenshot trail", 0.3)
    w.caption("A screenshot of each step, taken inside the sandbox.", 3)
    w.shot("receipt-trail")
    w.show("text=Network: the sandbox", 0.3)
    w.caption("The sandbox's only way out is a proxy that allows the form's own origin. Every request is logged.", 3.5)
    w.shot("receipt-network")

    # 4. Countersign
    w.show("#approve-card", 0.3)
    w.caption("Step 4: countersign. Approving signs this exact receipt.", 3)
    p.click("#approve")
    sub = w.wait_run()
    w.caption("A second fresh sandbox replays the fill. If it does not match the receipt you signed, nothing is submitted.", 1)
    rec = w.run_status(sub)
    if rec["status"] != "done":
        raise SystemExit(f"submit ended {rec['status']}: {rec.get('error')}")
    p.wait_for_selector("text=Submitted")
    p.wait_for_timeout(3500)  # the evidence bar refreshes every 3 s
    p.evaluate("window.scrollTo({top: 0})")
    w.caption("Submitted once. The model read the confirmation page. The bar below counts what the test site actually received.", 4.5)
    w.shot("submitted")
    w.show("text=Screenshot trail", 0.3)
    w.caption("Before and after Submit, from inside the second sandbox. Both sandboxes are already destroyed.", 3)
    w.shot("submitted-trail")

    # 5. Hostile page
    p.goto(w.base + "/#learn")
    p.wait_for_load_state("networkidle")
    w.caption("Now the containment moment: a hostile page.", 3)
    p.locator(".pick[data-url$='hostile.html']").click()
    w.caption("Hidden text orders the agent to tick boxes and leak data; its script posts whatever is typed to collector.test.", 1)
    p.locator("#learn").click()
    w.wait_hash("#tool/hostile", 180)
    p.goto(f"{w.base}/#grant/hostile")
    p.wait_for_selector("#issue")
    p.click("#issue")
    w.wait_text("Signed grant")
    p.click("text=Fill it now with this grant")
    p.wait_for_selector("#start")
    p.click("#start")
    hid = w.wait_run()
    w.caption("Same flow, fresh sandbox. The page attacks while the fields are typed.", 1)
    rec = w.run_status(hid)
    p.wait_for_selector("text=Receipt")
    p.wait_for_timeout(3500)
    p.evaluate("window.scrollTo({top: 0})")
    w.caption("Blocked: the page asks for $1,278 and the grant allows $0. That block cannot be approved away.", 4)
    w.shot("hostile-top")
    w.show("text=Evidence", 0.3)
    w.caption("The hidden instructions are quoted as evidence, not followed. The sponsor box stays unticked.", 4)
    w.shot("hostile-evidence")
    w.card("hostile-evidence", "Evidence")
    w.card("hostile-blocked", "#approve-card")
    w.show("text=Network: the sandbox", 0.3)
    w.caption("The proxy refused every attempt to reach collector.test, the cloud metadata address and the open internet.", 4)
    w.shot("hostile-network")
    w.card("hostile-network", "Network: the sandbox")
    w.caption("The collector received nothing: see the bar at the bottom.", 3.5)

    # 6. Another agent, out of scope
    state = w.api("/api/state")
    wiz = next(g for g in state["grants"] if g["form_id"] == "wizard")
    p.goto(w.base + "/#agents")
    p.wait_for_selector("#a-call")
    w.caption("Other agents use the same tools over MCP, with a grant. Here an agent holds a grant for the workshop form only.", 4)
    p.select_option("#a-tool", "hostile")
    p.fill("#a-token", wiz["token"])
    p.fill("#a-args", json.dumps({"full_name": "Ada Example", "email": "ada@example.org", "country": "PH", "pass": "all-access"}))
    p.click("#a-call")
    w.wait_text("Refused or failed", 60)
    w.show("#a-out", 0.3)
    w.caption("It asks for the hostile form instead. Refused at the boundary: no container was ever started.", 4.5)
    w.shot("agent-refused")
    w.card("agent-refused", "Try it as an agent")

    # 7. A statement only the owner can make
    p.goto(w.base + "/#learn")
    p.wait_for_load_state("networkidle")
    p.locator(".pick[data-url$='consent.html']").click()
    p.locator("#learn").click()
    w.wait_hash("#tool/consent", 180)
    p.goto(f"{w.base}/#grant/consent")
    p.wait_for_selector("#issue")
    p.fill("#g-max", "")
    p.click("#issue")
    w.wait_text("Signed grant")
    p.click("text=Fill it now with this grant")
    p.wait_for_selector("#start")
    p.click("#start")
    cid = w.wait_run()
    w.caption("Last: a form that demands a work-authorisation statement.", 1)
    w.run_status(cid)
    p.wait_for_selector("#approve-card")
    p.wait_for_timeout(800)
    w.show("#approve-card", 0.3)
    w.caption("Terms accepted, partner offers declined, and the statement is yours alone. Countersign never ticks it for you.", 5)
    w.shot("consent-owner")
    w.card("consent-owner", "#approve-card")
    p.goto(w.base + "/#run")
    p.wait_for_selector("text=Runs")
    w.caption("Every run, refusal and submission is on the record. Built for Vultr: VM control plane, Serverless Inference, a container per run.", 5)
    w.shot("runs")
    w.caption("", 0.5)


if __name__ == "__main__":
    sys.exit(main())
