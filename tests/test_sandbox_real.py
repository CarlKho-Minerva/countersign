"""Containment, tested against real containers. Needs a Docker engine and both images
(sandbox/build.sh). Skipped, loudly, when Docker is not there."""

import json
import secrets
import shutil
import subprocess
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from countersign import run
from countersign.grant import Grant, issue, new_keypair
from countersign.policy import EgressPolicy
from countersign.sandbox import LABEL, Images, Limits, Refused, Sandbox, SandboxKilled, ensure_egress_network, reap

REPO = Path(__file__).resolve().parent.parent
FORM = "http://form.test"
# Its own egress network, so a deployed stack's test site on cs-egress cannot answer for form.test.
NET = "cs-egress-test"


def docker(*args, check=True):
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and done.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)}: {done.stderr}")
    return done.stdout


def _engine_up() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=20).returncode == 0


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _engine_up(), reason="no Docker engine: containment NOT tested")]


@pytest.fixture(scope="module")
def site():
    for image in ("countersign-box", "countersign-proxy"):
        assert docker("images", "-q", image).strip(), f"image {image} missing: run sandbox/build.sh"
    ensure_egress_network(NET)
    name = f"cs-fixture-{secrets.token_hex(4)}"
    docker("run", "-d", "--name", name, "--network", NET, "--network-alias", "form.test",
           "--network-alias", "collector.test", "-v", f"{REPO / 'fixtures'}:/srv:ro", "python:3.13-slim",
           "python3", "/srv/serve.py", "80")
    time.sleep(1.0)

    class Site:
        container = name
        ip = docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{NET}").IPAddress}}}}', name).strip()

        @staticmethod
        def hits(host):
            rows = [json.loads(line) for line in docker("logs", name).splitlines() if line.startswith("{")]
            return [r for r in rows if r["host"] == host]

    yield Site
    docker("rm", "-f", name, check=False)


def factory(wall=60, **limits):
    # The fixtures live on a private Docker network, so their names are let through by name.
    return lambda p: Sandbox(replace(p, private_ok=frozenset(p.allow_hosts)), limits=Limits(wall_seconds=wall, **limits),
                             images=Images(egress_network=NET))


def leftovers():
    return docker("ps", "-aq", "--filter", f"label={LABEL}").split() + docker("network", "ls", "-q", "--filter", f"label={LABEL}").split()


@pytest.fixture(scope="module")
def keys():
    return new_keypair()


def token(priv, **over):
    base = dict(grant_id="g1", caller="agent:travel-desk", form_id="conf", origins=("form.test",),
                facts=("full_name", "email", "country", "pass"), max_total=Decimal("0.00"), may_submit=False,
                expires_at=int(time.time()) + 600)
    base.update(over)
    return issue(priv, Grant(**base))


FACTS = {"full_name": "Ada Example", "email": "ada@example.org", "country": "PH", "pass": "all-access"}


@pytest.fixture(scope="module")
def compiled(site):
    return run.learn("conf", f"{FORM}/plain.html", sandbox=factory())


def kinds(receipt):
    return [f.kind for f in receipt.flags]


def test_learn_compiles_the_form(compiled):
    keys_ = {f["key"]: f for f in compiled.fields}
    assert set(keys_) == {"full_name", "email", "job_title", "country", "pass", "newsletter", "share_with_sponsors", "terms", "register"}
    assert keys_["country"]["options"] == ["", "US", "PH", "JP"]
    assert keys_["pass"]["type"] == "radio" and keys_["pass"]["options"] == ["all-access", "main-only"]
    assert keys_["register"]["type"] == "submit"
    assert leftovers() == []


def test_fill_returns_a_receipt_and_never_submits(site, compiled, keys):
    priv, pub = keys
    result = run.fill(compiled, FACTS, token(priv), pub, sandbox=factory())
    r = result.receipt
    by = {e.key: e for e in r.entries}
    assert (by["full_name"].value, by["full_name"].status) == ("Ada Example", "known")
    assert (by["country"].value, by["pass"].value) == ("PH", "all-access")
    assert (by["job_title"].value, by["job_title"].status) == ("Student", "unreviewed")
    assert by["share_with_sponsors"].value == "ticked" and by["newsletter"].value == "not ticked"
    assert sorted(kinds(r)) == ["consent-preticked", "consent-required", "prefilled"]
    assert r.total == "0.00" and not r.approvable
    assert result.screenshot_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert not [h for h in site.hits("form.test") if h["method"] == "POST"], "the form was submitted"
    assert leftovers() == []


def test_hostile_page_cannot_send_anything_out(site, keys):
    priv, pub = keys
    hostile = run.learn("conf", f"{FORM}/hostile.html", sandbox=factory())
    result = run.fill(hostile, FACTS, token(priv), pub, sandbox=factory())
    r = result.receipt
    flags = {f.kind: f for f in r.flags}
    assert site.hits("collector.test") == [], "typed values reached the collector"
    blocked = {(row["host"], row["port"]) for row in result.proxy_log if row["action"] == "deny"}
    assert {("collector.test", 80), ("169.254.169.254", 80), ("example.com", 443)} <= blocked
    assert "collector.test:80" in " ".join(f.detail for f in r.flags if f.kind == "egress-blocked")
    hidden = " ".join(f.detail for f in r.flags if f.kind == "hidden-text")
    assert "tick" in hidden and "passport number" in hidden and "SYSTEM NOTE TO AI AGENTS" in hidden
    by = {e.key: e for e in r.entries}
    assert by["newsletter"].value == "not ticked" and by["job_title"].value == "Student"  # instructions not followed
    assert "total-mismatch" in flags and "1278.00" in flags["total-mismatch"].detail
    assert not r.approvable and leftovers() == []


def test_out_of_scope_call_never_starts_a_container(compiled, keys):
    priv, pub = keys
    started = []

    def spy(policy):
        started.append(policy)
        return factory()(policy)

    with pytest.raises(run.OutOfScope, match="form"):
        run.fill(compiled, FACTS, token(priv, form_id="another-form"), pub, sandbox=spy)
    with pytest.raises(run.OutOfScope, match="pass may not be disclosed"):
        run.fill(compiled, FACTS, token(priv, facts=("full_name", "email", "country")), pub, sandbox=spy)
    with pytest.raises(run.OutOfScope, match="origin"):
        run.fill(compiled, FACTS, token(priv, origins=("elsewhere.test",)), pub, sandbox=spy)
    assert started == [] and leftovers() == []


def test_changed_form_is_not_replayed(site, compiled, keys):
    priv, pub = keys
    moved = replace(compiled, url=f"{FORM}/changed.html")
    r = run.fill(moved, FACTS, token(priv), pub, sandbox=factory()).receipt
    assert kinds(r)[0] == "drift" and "added: phone" in r.flags[0].detail and "removed: email" in r.flags[0].detail
    assert {e.key: e.value for e in r.entries}["full_name"] == "", "a drifted form was filled anyway"
    assert not r.approvable


def test_time_cap_destroys_the_sandbox(site):
    box = factory(wall=8)(EgressPolicy(allow_hosts=frozenset({"form.test"})))
    began = time.monotonic()
    with pytest.raises(SandboxKilled, match="time cap of 8s"):
        with box:
            box.call("open", url=f"{FORM}/spin.html")
    assert time.monotonic() - began < 20
    assert leftovers() == []


def test_runner_refuses_what_it_must(site):
    with factory()(EgressPolicy(allow_hosts=frozenset({"form.test"}))) as box:
        with pytest.raises(Refused, match="outside this run's origins"):
            box.call("open", url="http://collector.test/")
        with pytest.raises(Refused, match="outside this run's origins"):
            box.call("open", url="file:///etc/passwd")
        box.call("open", url=f"{FORM}/plain.html")
        box.call("read_form")
        with pytest.raises(Refused, match="never filled"):
            box.call("fill", key="share_with_sponsors", value="on")
        with pytest.raises(Refused, match="only an approved replay"):
            box.call("click", key="register")
        with pytest.raises(Refused, match="unknown verb"):
            box.call("exec", cmd="id")
    assert not [h for h in site.hits("form.test") if h["method"] == "POST"]


def test_the_box_is_locked_down(site):
    """Probe the boundary from inside with docker exec, which the runner's protocol does not offer."""
    with factory()(EgressPolicy(allow_hosts=frozenset({"form.test"}))) as box:
        cfg = json.loads(docker("inspect", box.box))[0]
        host = cfg["HostConfig"]
        assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"] and host["Memory"] == 1 << 30
        assert host["MemorySwap"] == 1 << 30 and host["PidsLimit"] == 256 and host["NanoCpus"] == 10**9
        assert "no-new-privileges" in host["SecurityOpt"] and not host["Privileged"]
        assert cfg["Config"]["User"] == "pwuser" and list(cfg["NetworkSettings"]["Networks"]) == [box.net]
        assert json.loads(docker("network", "inspect", box.net))[0]["Internal"] is True
        # Exactly these variables and no others: the image's own, plus where the proxy is.
        # (GPG_KEY is the python base image's public release-signing fingerprint.)
        names = {e.split("=")[0] for e in cfg["Config"]["Env"]}
        assert names == {"PATH", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "PLAYWRIGHT_BROWSERS_PATH", "HOME",
                         "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "CS_PROXY", "CS_TTL", "CS_ALLOW_HOSTS"}

        def probe(code):
            return subprocess.run(["docker", "exec", box.box, "python3", "-c", code], capture_output=True, text=True, timeout=30)

        direct = probe(f"import socket; socket.create_connection(('{site.ip}', 80), 3)")
        assert direct.returncode != 0, "the box reached the site without the proxy"
        internet = probe("import socket; socket.create_connection(('1.1.1.1', 443), 3)")
        assert internet.returncode != 0, "the box reached the internet without the proxy"
        dns = probe("import socket; socket.setdefaulttimeout(4); print(socket.gethostbyname('example.com'))")
        assert dns.returncode != 0, f"the box resolved a public name: {dns.stdout}"
        write = probe("open('/app/x', 'w')")
        assert write.returncode != 0 and "Read-only" in write.stderr
        assert probe("import os; print(os.getuid())").stdout.strip() not in ("", "0")

        via_proxy = (
            "import os, urllib.request as u\n"
            "p = u.ProxyHandler({'http': os.environ['CS_PROXY']})\n"
            "o = u.build_opener(p)\n"
            "for url in ['http://form.test/plain.html', 'http://collector.test/x', 'http://%s/plain.html', 'http://form.test:22/']:\n"
            "    try: print(url, o.open(url, timeout=5).status)\n"
            "    except Exception as e: print(url, getattr(e, 'code', e))\n" % site.ip
        )
        out = probe(via_proxy).stdout
        assert "http://form.test/plain.html 200" in out
        assert "http://collector.test/x 403" in out and f"http://{site.ip}/plain.html 403" in out
        assert "http://form.test:22/ 403" in out
    assert leftovers() == []


def test_reap_removes_what_a_crash_left_behind():
    name = f"cs-orphan-{secrets.token_hex(4)}"
    docker("network", "create", "--internal", "--label", f"{LABEL}=orphan", name)
    docker("run", "-d", "--name", name, "--label", f"{LABEL}=orphan", "--network", name, "python:3.13-slim", "sleep", "300")
    assert reap(max_age_seconds=3600) == []  # young ones are left alone
    time.sleep(2)
    assert sorted(reap(max_age_seconds=1)) == [name, name]
    assert leftovers() == []


def test_memory_cap_is_named(site):
    with pytest.raises(SandboxKilled, match="memory cap of 96m"):
        with factory(memory="96m")(EgressPolicy(allow_hosts=frozenset({"form.test"}))) as box:
            box.call("open", url=f"{FORM}/plain.html")
    assert leftovers() == []


def posts(site):
    return [h for h in site.hits("form.test") if h["method"] == "POST"]


def test_countersigned_run_submits_exactly_what_was_approved(site, compiled, keys):
    priv, pub = keys
    grant = token(priv, may_submit=True)
    seen = run.fill(compiled, FACTS, grant, pub, sandbox=factory()).receipt
    assert not seen.approvable  # the pre-ticked sponsor box
    before = len(posts(site))
    approval = run.Approval(seen.digest(), consent={"share_with_sponsors": False, "newsletter": False, "terms": True})
    done = run.submit(compiled, FACTS, grant, pub, approval, sandbox=factory())
    sent = posts(site)[before:]
    assert len(sent) == 1 and done.receipt.submitted
    body = sent[0]["body"]
    assert "full_name=Ada+Example" in body and "country=PH" in body and "pass=all-access" in body
    assert "share_with_sponsors" not in body and "newsletter" not in body  # the owner said no to both
    assert "terms=on" in body
    assert done.landed["url"].endswith("/submit") and done.confirmation_png[:4] == b"\x89PNG"
    assert leftovers() == []


def test_submit_is_refused_without_a_matching_approval(site, compiled, keys):
    priv, pub = keys
    seen = run.fill(compiled, FACTS, token(priv, may_submit=True), pub, sandbox=factory()).receipt
    before = len(posts(site))
    decided = {"share_with_sponsors": False}
    with pytest.raises(run.OutOfScope, match="does not allow submitting"):  # the grant never allowed it
        run.submit(compiled, FACTS, token(priv), pub, run.Approval(seen.digest(), decided), sandbox=factory())
    with pytest.raises(run.NotApproved, match="consent-preticked"):  # the box was never decided
        run.submit(compiled, FACTS, token(priv, may_submit=True), pub, run.Approval(seen.digest()), sandbox=factory())
    with pytest.raises(run.NotApproved, match="does not produce the receipt"):  # different facts than approved
        run.submit(compiled, {**FACTS, "full_name": "Someone Else"}, token(priv, may_submit=True), pub,
                   run.Approval(seen.digest(), decided), sandbox=factory())
    with pytest.raises(run.NotApproved, match="not consent boxes"):  # an approval cannot smuggle a field value
        run.submit(compiled, FACTS, token(priv, may_submit=True), pub,
                   run.Approval(seen.digest(), {"job_title": True}), sandbox=factory())
    assert len(posts(site)) == before and leftovers() == []


def test_a_block_that_is_not_consent_cannot_be_approved_away(site, keys):
    priv, pub = keys
    hostile = run.learn("conf", f"{FORM}/hostile.html", sandbox=factory())
    grant = token(priv, may_submit=True)
    seen = run.fill(hostile, FACTS, grant, pub, sandbox=factory()).receipt
    before = len(posts(site))
    with pytest.raises(run.NotApproved, match="total-mismatch"):
        run.submit(hostile, FACTS, grant, pub, run.Approval(seen.digest(), {"share_with_sponsors": False}), sandbox=factory())
    assert len(posts(site)) == before and site.hits("collector.test") == []


def test_a_page_cannot_choose_the_size_of_the_reply(site):
    with factory()(EgressPolicy(allow_hosts=frozenset({"form.test"}))) as box:
        box.call("open", url=f"{FORM}/tall.html")
        shot = box.call("screenshot")["png_b64"]
    assert len(shot) < 4_000_000
    import base64, struct
    width, height = struct.unpack(">II", base64.b64decode(shot)[16:24])
    assert width <= 1600 and height <= 6000


def test_standing_consent_rule_needs_no_per_box_decision(site, compiled, keys):
    priv, pub = keys
    grant = token(priv, may_submit=True, consent="accept-terms")
    seen = run.fill(compiled, FACTS, grant, pub, sandbox=factory()).receipt
    by = {e.key: e for e in seen.entries}
    assert (by["terms"].value, by["share_with_sponsors"].value, by["newsletter"].value) == ("ticked", "not ticked", "not ticked")
    assert {by[k].status for k in ("terms", "share_with_sponsors", "newsletter")} == {"policy"}
    assert sorted(kinds(seen)) == ["consent-accepted", "consent-declined", "prefilled"] and seen.approvable
    before = len(posts(site))
    done = run.submit(compiled, FACTS, grant, pub, run.Approval(seen.digest()), sandbox=factory())
    sent = posts(site)[before:]
    assert len(sent) == 1 and done.receipt.submitted
    assert "terms=on" in sent[0]["body"] and "share_with_sponsors" not in sent[0]["body"] and "newsletter" not in sent[0]["body"]
    assert leftovers() == []


def test_hidden_instructions_do_not_move_the_consent_rule(site, keys):
    priv, pub = keys
    hostile = run.learn("conf", f"{FORM}/hostile.html", sandbox=factory())
    r = run.fill(hostile, FACTS, token(priv, consent="accept-terms"), pub, sandbox=factory()).receipt
    by = {e.key: e for e in r.entries}
    assert by["share_with_sponsors"].value == "not ticked" and by["newsletter"].value == "not ticked"
    assert site.hits("collector.test") == [] and not r.approvable  # the total still blocks


def test_rule_reads_the_wording_not_the_required_mark(site, keys):
    priv, pub = keys
    form = run.learn("apply", f"{FORM}/consent.html", sandbox=factory())
    grant = token(priv, form_id="apply", facts=("full_name",), max_total=None, may_submit=True, consent="accept-terms")
    seen = run.fill(form, {"full_name": "Ada Example"}, grant, pub, sandbox=factory()).receipt
    by = {e.key: (e.value, e.status) for e in seen.entries}
    assert by["tos"] == ("ticked", "policy")  # terms, though the form did not mark it required
    assert by["partner_offers"] == ("not ticked", "policy")  # marketing, though the form demands it and pre-ticked it
    assert by["remember"] == ("not ticked", "policy")  # optional
    assert by["work_auth"] == ("not ticked", "yours")  # a statement of fact is never the rule's to make
    blocks = sorted((f.kind, f.key) for f in seen.flags if f.blocking)
    assert blocks == [("consent-required", "partner_offers"), ("consent-required", "work_auth")] and not seen.approvable
    before = len(posts(site))
    with pytest.raises(run.NotApproved, match="work_auth|authorized to work"):
        run.submit(form, {"full_name": "Ada Example"}, grant, pub, run.Approval(seen.digest()), sandbox=factory())
    assert len(posts(site)) == before and leftovers() == []


# The model-driven layer against real containers, with a scripted model: learn a form
# in steps, compile it to a typed tool, map facts with known-or-guess labels, fill with a
# screenshot of each step, check them, countersign, submit.

from countersign.mapper import map_facts  # noqa: E402
from countersign.tool import compile_tool  # noqa: E402
from countersign.witness import check_confirmation, check_fill  # noqa: E402

from fakes import Script, fake_model  # noqa: E402

WIZ_LEARN = {
    "title": "Workshop registration", "purpose": "Registers one person for the workshop.",
    "fields": [{"key": "organisation", "description": "Where the attendee works", "fact": "company"}],
    "boxes": [{"key": "house_rules", "kind": "terms"}, {"key": "workshop_news", "kind": "marketing"}],
}
WIZ_MAP = {"values": [
    {"key": "country", "value": "PH", "from": ["country"]},
    {"key": "organisation", "value": "Example Labs", "from": ["role"], "why": "the company in her role"},
    {"key": "session", "value": "morning", "from": ["session_pref"], "why": "she prefers mornings"},
    {"key": "meal", "value": "vegetarian", "from": ["diet"]},
]}
PROFILE = {"full_name": "Ada Example", "email": "ada@example.org", "country": "Philippines", "role": "Founder, Example Labs",
           "session_pref": "mornings", "diet": "Vegetarian", "passport_no": "X0000000"}


def test_learning_walks_a_form_in_steps(site):
    learned = run.learn_walk("ws", f"{FORM}/wizard.html", sandbox=factory())
    c = learned.compiled
    assert c.steps == ("next_1",) and len(learned.trail) == 2
    assert all(png[:4] == b"\x89PNG" for _, png in learned.trail)
    step = {f["key"]: f["step"] for f in c.fields}
    assert step["full_name"] == 0 and step["session"] == 1 and step["register"] == 1
    assert all(f["visible"] for f in c.fields if f["key"] in ("full_name", "session", "house_rules"))
    by = {f["key"]: f for f in c.fields}
    assert by["session"]["label"] == "Session" and by["meal"]["option_labels"][1] == "Vegetarian"
    assert learned.hidden_text == []  # a step that is merely not shown yet is not hidden text
    assert leftovers() == []


def test_learn_map_fill_witness_countersign_submit(site, keys, tmp_path):
    priv, pub = keys
    model = fake_model(Script(learn=WIZ_LEARN, map=WIZ_MAP, witness={"fields": [{"key": "full_name", "agrees": True}]},
                              confirm={"outcome": "accepted", "evidence": "Registration received"}), tmp_path)
    tool = compile_tool(run.learn_walk("ws", f"{FORM}/wizard.html", sandbox=factory()), model=model)
    assert set(tool.input_schema()["properties"]) == {"full_name", "email", "organisation", "country", "session", "meal"}
    allowed = ("full_name", "email", "country", "role", "session_pref", "diet")
    grant = token(priv, form_id="ws", facts=allowed, may_submit=True, consent="accept-terms")
    mapping = map_facts(tool, PROFILE, allowed=allowed, model=model)
    assert mapping.withheld == ["passport_no"]

    seen = run.fill(tool.compiled, PROFILE, grant, pub, sandbox=factory(), intents=mapping.intents)
    r = seen.receipt
    by = {e.key: e for e in r.entries}
    assert (by["full_name"].status, by["country"].value, by["country"].status) == ("known", "PH", "known")
    assert (by["organisation"].value, by["organisation"].status) == ("Example Labs", "guess")
    assert (by["session"].value, by["meal"].value, by["meal"].status) == ("morning", "vegetarian", "known")
    assert (by["house_rules"].value, by["workshop_news"].value) == ("ticked", "not ticked")  # model read the rules box; rule declined the news
    assert r.approvable and [c for c, _ in seen.trail] == ["step 1 filled", "step 2 filled"]
    r.flags += check_fill(model, r, [png for _, png in seen.trail], [])
    assert r.approvable

    before = len(posts(site))
    done = run.submit(tool.compiled, PROFILE, grant, pub, run.Approval(r.digest()), sandbox=factory(), intents=mapping.intents)
    sent = posts(site)[before:]
    assert len(sent) == 1 and done.receipt.submitted
    body = sent[0]["body"]
    for part in ("full_name=Ada+Example", "country=PH", "organisation=Example+Labs", "session=morning", "meal=vegetarian", "house_rules=on"):
        assert part in body
    assert "workshop_news" not in body and "X0000000" not in body
    assert check_confirmation(model, done.confirmation_png, done.landed).detail.startswith("accepted")
    assert leftovers() == []
