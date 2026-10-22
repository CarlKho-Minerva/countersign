"""The web app and agent API, without containers: everything decided before a sandbox
starts, and who may see and do what."""

import json

import pytest
from fastapi.testclient import TestClient

from countersign import run
from countersign.fingerprint import fingerprint
from countersign.tool import compile_tool
from countersign.web.app import DEMO_PROFILE, create_app
from countersign.web.service import Service, _write_json

from fakes import Script, fake_model
from test_tool_mapper import FIELDS


def no_sandbox(policy):
    raise AssertionError(f"a container was started for {policy}")


def make(tmp_path, password=None, demo=True):
    svc = Service(tmp_path / "data", model=None, sandbox=no_sandbox, demo=demo, profile=DEMO_PROFILE)
    compiled = run.Compiled("wizard", "http://form.test/wizard.html", "form.test", FIELDS, fingerprint(FIELDS), ("next_1",))
    tool = compile_tool(run.Learned(compiled, [], [], "Workshop"),
                        model=fake_model(Script(learn={"title": "Workshop registration"}), tmp_path))
    _write_json(svc.data / "tools" / "wizard.json", tool.to_dict())
    return svc, TestClient(create_app(svc, password=password))


def grant(client, **over):
    body = dict(form_id="wizard", caller="agent:travel-desk", facts=["full_name", "email", "country"], max_total="0",
                may_submit=False, consent="accept-terms", ttl_minutes=30)
    body.update(over)
    r = client.post("/api/grants", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_state_lists_the_learned_tool_and_the_demo_profile(tmp_path):
    _, c = make(tmp_path)
    s = c.get("/api/state").json()
    assert s["demo"] and s["owner"] and s["model"] is None
    assert [t["name"] for t in s["tools"]] == ["fill_wizard"]
    assert s["profile"]["full_name"] == "Ada Example"
    assert "http://form.test/wizard.html" in s["fixtures"]


def test_demo_refuses_to_learn_anything_but_its_own_forms(tmp_path):
    _, c = make(tmp_path)
    r = c.post("/api/learn", json={"url": "https://example.com/signup"})
    assert r.status_code == 403 and "only learns its bundled forms" in r.json()["detail"]


def test_bad_arguments_are_refused_before_a_job_exists(tmp_path):
    _, c = make(tmp_path)
    g = grant(c)
    r = c.post("/api/tools/wizard/call", json={"full_name": "Ada", "house_rules": "on"},
               headers={"Authorization": f"Bearer {g['token']}"})
    assert r.status_code == 400 and "house_rules: not a parameter" in r.json()["detail"]


def test_a_grant_for_another_form_is_refused_at_the_boundary(tmp_path):
    svc, c = make(tmp_path)
    _write_json(svc.data / "tools" / "other.json", {**svc.tool("wizard").to_dict(), "form_id": "other",
                                                      "compiled": {**svc.tool("wizard").to_dict()["compiled"], "form_id": "other"}})
    g = grant(c, form_id="other")
    args = {"full_name": "Ada Example", "email": "ada@example.org", "country": "PH"}
    r = c.post("/api/tools/wizard/call", json=args, headers={"Authorization": f"Bearer {g['token']}"})
    assert r.status_code == 403 and "refused before any container started" in r.json()["detail"]
    assert "form: grant" in r.json()["detail"]
    runs = c.get("/api/state").json()["runs"]
    assert runs[0]["status"] == "refused"  # the owner sees the attempt


def test_a_forged_or_missing_grant_is_refused(tmp_path):
    _, c = make(tmp_path)
    args = {"full_name": "Ada Example", "email": "ada@example.org", "country": "PH"}
    assert c.post("/api/tools/wizard/call", json=args).status_code == 401
    r = c.post("/api/tools/wizard/call", json=args, headers={"Authorization": "Bearer e30.e30"})
    assert r.status_code == 403


def test_mcp_lists_typed_tools_and_refuses_calls_without_a_grant(tmp_path):
    _, c = make(tmp_path)
    init = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json()
    assert init["result"]["serverInfo"]["name"] == "countersign"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert tools[0]["name"] == "fill_wizard" and tools[0]["inputSchema"]["properties"]["country"]["enum"] == ["US", "PH"]
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fill_wizard", "arguments": {}}}
    res = c.post("/mcp", json=call).json()["result"]
    assert res["isError"] and "Authorization" in res["content"][0]["text"]
    g = grant(c, form_id="wizard", facts=["full_name"])
    call["params"]["arguments"] = {"full_name": "Ada Example", "email": "ada@example.org", "country": "PH"}
    res = c.post("/mcp", json=call, headers={"Authorization": f"Bearer {g['token']}"}).json()["result"]
    assert res["isError"] and "email may not be disclosed" in res["content"][0]["text"]
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "resources/list"}).json()["error"]["code"] == -32601


def test_a_real_deployment_needs_a_password_and_hides_everything_without_it(tmp_path):
    svc, c = make(tmp_path, password="correct horse", demo=False)
    s = c.get("/api/state").json()
    assert s["needs_login"] and s["runs"] == [] and s["grants"] == [] and s["profile"] == {}
    assert c.post("/api/grants", json={"form_id": "wizard"}).status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"password": "correct horse"}).status_code == 200
    assert c.get("/api/state").json()["owner"] is True
    assert grant(c)["grant_id"].startswith("g-")


def test_tokens_never_appear_in_a_run_and_approval_needs_the_receipt_shown(tmp_path):
    svc, c = make(tmp_path)
    g = grant(c, may_submit=True)
    rec = svc._new("fill", form_id="wizard", caller="x", grant_id=g["grant_id"], token=g["token"], status="done",
                   receipt={"digest": "abc", "approvable": True, "entries": [], "flags": []},
                   mapping={"intents": {}})
    svc._save({**rec, "status": "done"})
    body = c.get(f"/api/runs/{rec['id']}").json()
    assert "token" not in body and g["token"] not in json.dumps(body)
    r = c.post(f"/api/runs/{rec['id']}/approve", json={"digest": "not-it", "consent": {}})
    assert r.status_code == 403 and "not the receipt you were shown" in r.json()["detail"]


def test_the_model_never_sees_facts_outside_the_grant(tmp_path):
    svc, c = make(tmp_path)
    script = Script(map={"values": []}, witness={})
    svc.model = fake_model(script, tmp_path)
    g = grant(c, facts=["full_name", "email"])
    svc.sandbox = no_sandbox
    rec = c.post("/api/runs", json={"form_id": "wizard", "grant_id": g["grant_id"]}).json()
    done = svc.wait(rec["id"], timeout=10)
    assert done["status"] == "failed" and "a container was started" in done["error"]  # it got as far as the sandbox
    sent = script.requests[0]["body"]["messages"][1]["content"]
    facts_sent = json.loads(sent.split("Facts (key: value):")[1].split("Return:")[0])
    assert facts_sent == {"full_name": "Ada Example", "email": "ada@example.org"}
    assert "X0000000" not in json.dumps(script.requests)
    assert set(done["mapping"]["withheld"]) == {"country", "role", "session_pref", "diet", "passport_no"}


def test_two_approvals_of_one_receipt_never_mean_two_submissions(tmp_path):
    import threading
    svc, c = make(tmp_path)
    g = grant(c, may_submit=True)
    rec = svc._new("fill", form_id="wizard", caller="x", grant_id=g["grant_id"], token=g["token"], status="done",
                   receipt={"digest": "abc", "approvable": True, "entries": [], "flags": []}, mapping={"intents": {}})
    started = []
    svc._start = lambda r, work: started.append(r) or r  # count submits without running them
    results = []

    def click():
        try:
            results.append(svc.approve(rec["id"], "abc", {})["id"])
        except Exception as exc:  # noqa: BLE001
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=click) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(started) == 1 and results.count("Forbidden") == 7


def test_one_address_cannot_queue_unlimited_runs(tmp_path):
    from countersign.web.app import RateLimit
    svc, _ = make(tmp_path)
    c = TestClient(create_app(svc, limit=RateLimit(2, 600)))
    codes = [c.post("/api/learn", json={"url": "https://example.com/x"}).status_code for _ in range(3)]
    assert codes == [403, 403, 429]  # the first two reach the demo check; the third is never let in


def test_the_spend_ledger_lives_with_the_data_so_the_cap_survives_a_redeploy(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CS_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("CS_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("CS_MODEL_API_KEY", "k")
    monkeypatch.setenv("CS_MODEL", "m")
    monkeypatch.setenv("CS_REAP", "0")
    monkeypatch.delenv("CS_LEDGER", raising=False)
    app = create_app()
    assert app.state.service.model.ledger == tmp_path / "data" / "spend.jsonl"
