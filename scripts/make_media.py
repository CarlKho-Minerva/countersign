"""Build the submission media from real artefacts: the cover image, the slide deck (PDF)
and the demo video (MP4 with title and closing cards).

    uv run --with playwright python scripts/make_media.py --shots dist/walkthrough --video-dir dist/video --out dist

The screenshots come from scripts/walkthrough.py (stills) and the recording from the same
script with --video. Nothing here invents a number: the few figures on the slides are
passed in with --facts, a JSON file written from real runs.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1600, 900

CSS = """
* { box-sizing: border-box; margin: 0; }
body { font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; color: #1c1917; background: #fafaf9; }
.slide { width: 1600px; height: 900px; padding: 70px 90px; position: relative; overflow: hidden; page-break-after: always; background: #fafaf9; }
.slide.dark { background: #1c1917; color: #fafaf9; }
.kicker { font: 600 18px/1 ui-monospace, Menlo, monospace; letter-spacing: .08em; text-transform: uppercase; color: #78716c; margin-bottom: 26px; }
.dark .kicker { color: #a8a29e; }
h1 { font-size: 64px; letter-spacing: -0.02em; line-height: 1.05; }
h2 { font-size: 44px; letter-spacing: -0.015em; line-height: 1.1; margin-bottom: 30px; }
p, li { font-size: 25px; line-height: 1.45; }
.sub { font-size: 30px; color: #57534e; margin-top: 22px; max-width: 1200px; }
.dark .sub { color: #d6d3d1; }
ul { padding-left: 28px; } li { margin: 10px 0; }
.mark { display: inline-grid; place-items: center; width: 64px; height: 64px; border-radius: 14px; background: #1c1917; color: #fafaf9; font-size: 38px; vertical-align: middle; margin-right: 18px; }
.dark .mark { background: #fafaf9; color: #1c1917; }
.foot { position: absolute; left: 90px; right: 90px; bottom: 44px; font-size: 18px; color: #78716c; display: flex; justify-content: space-between; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 48px; align-items: start; }
.cols3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 28px; }
.shot { width: 100%; border: 1px solid #e7e5e4; border-radius: 10px; box-shadow: 0 10px 40px rgba(28,25,23,.10); background: #fff; }
.box { background: #fff; border: 1px solid #e7e5e4; border-radius: 12px; padding: 22px 24px; }
.box b { display: block; font-size: 24px; margin-bottom: 6px; }
.box span { font-size: 20px; color: #57534e; line-height: 1.4; display: block; }
.flow { display: flex; gap: 14px; align-items: stretch; margin-top: 10px; }
.flow .box { flex: 1; }
.flow .arrow { align-self: center; font-size: 34px; color: #a8a29e; }
.big { font-size: 96px; font-weight: 700; letter-spacing: -0.03em; line-height: 1; }
.green { color: #15803d; } .red { color: #b91c1c; } .purple { color: #6b21a8; } .amber { color: #92400e; }
.stat { font-size: 21px; color: #57534e; margin-top: 8px; }
.tag { display: inline-block; padding: 4px 14px; border-radius: 999px; font-size: 18px; font-weight: 600; margin-right: 8px; }
.t-known { background: #dcfce7; color: #166534; } .t-guess { background: #fef3c7; color: #92400e; }
.t-policy { background: #dbeafe; color: #1e40af; } .t-yours { background: #e7e5e4; color: #44403c; } .t-model { background: #f3e8ff; color: #6b21a8; }
pre { font: 17px/1.45 ui-monospace, Menlo, monospace; background: #1c1917; color: #f5f5f4; padding: 22px 26px; border-radius: 12px; white-space: pre; }
.card-img { width: 100%; border: 1px solid #e7e5e4; border-radius: 12px; box-shadow: 0 10px 40px rgba(28,25,23,.10); background: #fff; display: block; }
.stack { display: flex; flex-direction: column; gap: 16px; }
.honest { border-left: 6px solid #1c1917; padding-left: 26px; }
"""


def img(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def page_html(slides: list[str]) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(slides)}</body></html>"


def foot(n: int, total: int) -> str:
    return f"<div class='foot'><span>Countersign &middot; Vultr Agent Rush, Blast Radius Zero, pattern B</span><span>{n} / {total}</span></div>"


def deck(shots: Path, facts: dict) -> list[str]:
    s = {p.stem.split("-", 1)[1]: p for p in sorted(shots.glob("[0-9]*.png"))}
    c = {p.stem[len("card-"):]: p for p in shots.glob("card-*.png")}
    slides = [
        f"""<section class='slide dark'><div class='kicker'>Vultr Agent Rush &middot; lablab.ai &middot; Blast Radius Zero &middot; pattern B, sandboxed browser use</div>
        <h1 style='margin-top:150px'><span class='mark'>&#10003;</span>Countersign</h1>
        <p class='sub' style='font-size:38px'>Forms your agents can fill, in a sandbox, with a receipt you sign.</p>
        <p class='sub' style='margin-top:120px;font-size:24px'>Carl Kho &middot; team Just Carl</p></section>""",
        """<section class='slide'><div class='kicker'>The problem</div><h2>An agent that fills a form for you makes mistakes you cannot see.</h2>
        <ul><li>A job title arrives prefilled and stays wrong.</li><li>A sponsor-sharing box arrives ticked and stays ticked.</li>
        <li>The total says <b>$1,278</b> where you expected $0.</li><li>The page carries hidden text that tells the agent what to do, and a script that sends what is typed somewhere else.</li>
        <li>And every run pays a vision model to rediscover a form it has filled before.</li></ul></section>""",
        """<section class='slide'><div class='kicker'>What it does</div><h2>Learn once. Fill under a grant. Stop before Submit. You countersign.</h2>
        <div class='flow'>
        <div class='box'><b>1 Learn</b><span>A sandboxed browser walks the form, steps and all, never pressing Submit. The model compiles it into a typed tool.</span></div><div class='arrow'>&rarr;</div>
        <div class='box'><b>2 Grant</b><span>A signed, expiring grant: which caller, which form, which facts, the largest total, the consent rule.</span></div><div class='arrow'>&rarr;</div>
        <div class='box'><b>3 Fill</b><span>The model maps your allowed facts to fields. A fresh sandbox fills every step and stops before Submit.</span></div><div class='arrow'>&rarr;</div>
        <div class='box'><b>4 Receipt</b><span>Every field, its source, known or guessed; every box and what decided it; everything the page tried.</span></div><div class='arrow'>&rarr;</div>
        <div class='box'><b>5 Countersign</b><span>A second fresh sandbox replays the fill. Only if it matches the receipt you approved does it press Submit.</span></div></div>
        <p class='sub' style='font-size:24px;margin-top:40px'>A person uses the web app. Another agent uses the same typed tool over MCP, with a grant. An agent can get a receipt; only the owner can approve one.</p></section>""",
        f"""<section class='slide'><div class='kicker'>The receipt</div><h2 style='margin-bottom:16px'>Every value says where it came from.</h2>
        <p style='font-size:21px'><span class='tag t-known'>known</span> your fact, or the form's option whose label is your fact: checked by code &nbsp;
        <span class='tag t-guess'>guess</span> worked out by the model, with its reason &nbsp; <span class='tag t-policy'>policy</span> your consent rule &nbsp;
        <span class='tag t-yours'>yours</span> never filled for you</p>
        <img class='card-img' src='{img(c['receipt'])}' style='margin-top:22px;height:500px;object-fit:cover;object-position:top'></section>""",
        f"""<section class='slide'><div class='kicker'>The containment moment</div><h2>A hostile page, contained.</h2><div class='cols' style='grid-template-columns:1.45fr 1fr;gap:40px'>
        <div class='stack'><img class='card-img' src='{img(c['hostile-blocked'])}'><img class='card-img' src='{img(c['hostile-network'])}'></div>
        <div style='font-size:22px'><p style='font-size:22px'>Hidden text orders the agent to tick the sponsor box and send the applicant's facts to collector.test. Its script posts every keystroke there.</p>
        <ul style='margin-top:18px'><li>The proxy refused collector.test <b>{facts.get('collector_refusals', 'every')}</b> times, the cloud metadata address and the open internet.</li>
        <li>The collector received <b class='green'>0</b> requests.</li><li>The instructions are quoted on the receipt, not followed. The sponsor box stays unticked.</li>
        <li>The <b class='red'>$1,278</b> total blocks approval, and no approval can lift that block.</li>
        <li>An agent whose grant names another form is refused before a container starts.</li></ul></div></div></section>""",
        """<section class='slide'><div class='kicker'>Architecture</div>
<pre>browser (owner)  ──►  control plane on a Vultr VM            ──►  Vultr Serverless Inference
other agents     ──►  FastAPI web app, MCP, grants, receipts       (every model call; the key lives only here)
 (MCP + grant)               │ fixed verbs over stdio: open, read_form, fill, set_consent, click, screenshot
                             ▼
          ┌─ one internal Docker network per run ───────────────────────────┐
          │ Chromium + runner: non-root, read-only root, no capabilities,   │
          │ no DNS, no secrets; 1 CPU, 1 GB, 256 processes, 60 s            │
          │        │ the only way out                                       │
          │ egress proxy: the grant's origins by name; never an address,    │──►  the form's origin
          │ never a private range; logs every allow and refusal             │
          └─────────────────────────────────────────────────────────────────┘
            destroyed after every run, pass or fail; a reaper removes leftovers</pre>
        <div class='cols3' style='margin-top:30px'>
        <div class='box'><b>Process isolation</b><span>The browser never runs in the app or on the host. Chromium's own sandbox stays on inside the container.</span></div>
        <div class='box'><b>Secret hygiene</b><span>The box gets the facts the grant allows and the proxy's address. No keys, no cookies, no passwords.</span></div>
        <div class='box'><b>Limits and lifecycle</b><span>Memory, CPU, processes and wall time on every run; hitting one destroys it and the receipt says which.</span></div></div></section>""",
        """<section class='slide'><div class='kicker'>Where the model helps, and where it is not trusted</div><h2>The model proposes. Code decides.</h2>
        <div class='cols3'>
        <div class='box'><b>Reads the form</b><span>Titles, descriptions, which fact each field wants, how each consent box reads. Types, choices and keys come from the page, so a tool cannot name a field that does not exist.</span></div>
        <div class='box'><b>Maps your facts</b><span>It only ever sees the facts the grant allows. A value is "known" only if code can check it against your fact; everything else is a labelled guess.</span></div>
        <div class='box'><b>Checks the screenshots</b><span>A second witness for each step and for the page after Submit. It can warn; it can never approve or lift a block, because a page can write text aimed at it.</span></div></div>
        <p class='sub' style='font-size:23px;margin-top:34px'>Consent: the model may read an oddly worded rules box as terms. It can never tick marketing, and never a statement only you can make, such as work authorisation. When the provider's own filter saw the hostile page's text as a prompt attack, that became a finding on the receipt.</p></section>""",
        f"""<section class='slide'><div class='kicker'>For other agents</div><div class='cols' style='grid-template-columns:1fr 1.45fr'>
        <div><h2>Every learned form is a typed tool.</h2><p>Listed over MCP with its JSON Schema. Called with the owner's grant. Replayed without a vision model while the form keeps its shape; a changed form is refused and learned again.</p>
        <p style='margin-top:18px'>Here an agent holds a grant for the workshop form and asks for the hostile one: <b class='red'>refused before any container started</b>.</p></div>
        <img class='card-img' src='{img(c['agent-refused'])}'></div></section>""",
        """<section class='slide'><div class='kicker'>Business value</div><h2>Agents can do the paperwork. People keep the signature.</h2>
        <div class='cols3'>
        <div class='box'><b>For people</b><span>Registrations, applications, renewals: hand them to an agent and approve a receipt instead of re-reading every field.</span></div>
        <div class='box'><b>For teams and platforms</b><span>A form-filling tool other agents can call, with scoped grants, a spend limit on totals, and an audit trail of every run and refusal.</span></div>
        <div class='box'><b>Cheaper over time</b><span>Learn a form once; replays need no vision model until the form changes shape.</span></div></div>
        <p class='sub' style='font-size:23px;margin-top:34px'>Not in scope: login walls, CAPTCHAs, payments and stored passwords. Those stay the owner's.</p></section>""",
        f"""<section class='slide'><div class='kicker'>Built when, and how it was checked</div><h2>Honest about the clock.</h2>
        <div class='cols'><div class='honest'>
        <p>The containment layer (grants, sandbox, proxy, drift check, receipt) was written on <b>2026-10-13</b>, before kickoff, with no model in it.</p>
        <p style='margin-top:16px'>The model-driven parts (learning a form, mapping facts, checking screenshots), the web app, the MCP endpoint, this deck and the video were built on <b>2026-10-21</b>, also before the Nov 3 kickoff. Commit dates are real.</p>
        <p style='margin-top:16px'>The recording runs on a local Docker host with {facts.get('model_label', 'a development model')}. The production configuration is a Vultr VM and Vultr Serverless Inference, deployed with one command.</p></div>
        <div><div class='big green'>{facts.get('tests', '?')}</div><div class='stat'>tests pass, {facts.get('docker_tests', '?')} of them against real containers</div>
        <div class='big' style='margin-top:30px'>{facts.get('demo_checks', '?')}</div><div class='stat'>checks in the end-to-end demo against the bundled forms</div>
        <div class='big' style='margin-top:30px'>${facts.get('demo_cost', '?')}</div><div class='stat'>model cost of one full demo run</div></div></div></section>""",
    ]
    total = len(slides)
    return [sl.replace("</section>", foot(i + 1, total) + "</section>", 1) if i else sl for i, sl in enumerate(slides)]


def cards(facts: dict) -> tuple[str, str]:
    intro = f"""<section class='slide dark'><div class='kicker'>Vultr Agent Rush &middot; Blast Radius Zero &middot; sandboxed browser use</div>
      <h1 style='margin-top:160px'><span class='mark'>&#10003;</span>Countersign</h1>
      <p class='sub' style='font-size:38px'>Forms your agents can fill, in a sandbox, with a receipt you sign.</p>
      <p class='sub' style='margin-top:110px;font-size:24px'>A screen recording of the working app. No voice; captions only.</p></section>"""
    outro = f"""<section class='slide dark'><div class='kicker'>What you just saw</div>
      <ul style='margin-top:40px'><li>A form learned once and compiled into a typed tool, steps and consent boxes included.</li>
      <li>Facts mapped by a model that only sees what the grant allows; every value known or labelled a guess.</li>
      <li>A fresh sandbox per run; Submit only after you countersign, in a second sandbox.</li>
      <li>A hostile page contained: the collector received 0 requests.</li></ul>
      <p class='sub' style='font-size:22px;margin-top:60px'>Recorded on a local Docker host with {facts.get('model_label', 'a development model')}. Production: a Vultr VM and Vultr Serverless Inference.
      The model-driven parts and this app were built on 2026-10-21, before the Nov 3 kickoff.</p></section>"""
    return intro, outro


def render(pw, html: str, png: Path | None = None, pdf: Path | None = None) -> None:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": W, "height": H})
    page.set_content(html, wait_until="load")
    page.wait_for_timeout(300)
    if png:
        page.screenshot(path=str(png))
    if pdf:
        page.pdf(path=str(pdf), width=f"{W}px", height=f"{H}px", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    browser.close()


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="dist/walkthrough")
    ap.add_argument("--video-dir", default="dist/video")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--facts", default="dist/facts.json")
    a = ap.parse_args()
    out, shots = Path(a.out), Path(a.shots)
    out.mkdir(parents=True, exist_ok=True)
    facts = json.loads(Path(a.facts).read_text()) if Path(a.facts).exists() else {}
    with sync_playwright() as pw:
        slides = deck(shots, facts)
        render(pw, page_html(slides), pdf=out / "countersign-deck.pdf")
        render(pw, page_html([slides[0].replace("<section class='slide dark'>", "<section class='slide dark' style='page-break-after:auto'>")]),
               png=out / "countersign-cover.png")
        intro, outro = cards(facts)
        tmp = out / "cards"
        tmp.mkdir(exist_ok=True)
        render(pw, page_html([intro]), png=tmp / "intro.png")
        render(pw, page_html([outro]), png=tmp / "outro.png")
    raws = sorted(Path(a.video_dir).glob("raw/*.webm"))
    if raws:
        enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", "-crf", "20", "-preset", "medium"]
        ffmpeg("-loop", "1", "-t", "5", "-i", str(tmp / "intro.png"), *enc, str(tmp / "intro.mp4"))
        ffmpeg("-loop", "1", "-t", "8", "-i", str(tmp / "outro.png"), *enc, str(tmp / "outro.mp4"))
        ffmpeg("-i", str(raws[-1]), *enc, str(tmp / "main.mp4"))
        (tmp / "list.txt").write_text("".join(f"file '{n}'\n" for n in ("intro.mp4", "main.mp4", "outro.mp4")))
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(out / "countersign-demo.mp4"))
        captions = Path(a.video_dir) / "captions.json"
        if captions.exists():
            shutil.copy(captions, out / "countersign-demo-captions.json")
    print(f"media in {out}")


if __name__ == "__main__":
    main()
