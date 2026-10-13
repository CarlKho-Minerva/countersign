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
from countersign.sandbox import LABEL, Limits, Refused, Sandbox, SandboxKilled, ensure_egress_network, reap

REPO = Path(__file__).resolve().parent.parent
FORM = "http://form.test"


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
    ensure_egress_network()
    name = f"cs-fixture-{secrets.token_hex(4)}"
    docker("run", "-d", "--name", name, "--network", "cs-egress", "--network-alias", "form.test",
           "--network-alias", "collector.test", "-v", f"{REPO / 'fixtures'}:/srv:ro", "python:3.13-slim",
           "python3", "/srv/serve.py", "80")
    time.sleep(1.0)

    class Site:
        container = name
        ip = docker("inspect", "-f", '{{(index .NetworkSettings.Networks "cs-egress").IPAddress}}', name).strip()

        @staticmethod
        def hits(host):
            rows = [json.loads(line) for line in docker("logs", name).splitlines() if line.startswith("{")]
            return [r for r in rows if r["host"] == host]

    yield Site
    docker("rm", "-f", name, check=False)


def factory(wall=60, **limits):
    # The fixtures live on a private Docker network, so their names are let through by name.
    return lambda p: Sandbox(replace(p, private_ok=frozenset(p.allow_hosts)), limits=Limits(wall_seconds=wall, **limits))


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
    assert set(keys_) == {"full_name", "email", "job_title", "country", "pass", "newsletter", "share_with_sponsors", "register"}
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
    assert sorted(kinds(r)) == ["consent-preticked", "prefilled"]
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
    approval = run.Approval(seen.digest(), consent={"share_with_sponsors": False, "newsletter": False})
    done = run.submit(compiled, FACTS, grant, pub, approval, sandbox=factory())
    sent = posts(site)[before:]
    assert len(sent) == 1 and done.receipt.submitted
    body = sent[0]["body"]
    assert "full_name=Ada+Example" in body and "country=PH" in body and "pass=all-access" in body
    assert "share_with_sponsors" not in body and "newsletter" not in body  # the owner said no to both
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
