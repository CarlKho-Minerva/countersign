"""The web app: the owner's pages, the agent API and an MCP endpoint, on one FastAPI app.

Run it with `python -m countersign.web.app`. Configuration is
read from the environment; see `.env.example`.

Who may do what:
- The owner (a session cookie after the password, or anyone in demo mode) learns forms,
  issues grants, runs fills and approves receipts.
- Another agent, holding a grant token, may list tools and call one. A call returns a
  receipt; it can never approve one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import run
from ..grant import GrantError, verify
from ..model import Model, ModelError
from ..tool import ToolCallError
from .service import Busy, Forbidden, NotFound, Service

STATIC = Path(__file__).parent / "static"
DEMO_PROFILE = {
    # A made-up person. Nothing here is anyone's real data.
    "full_name": "Ada Example",
    "email": "ada@example.org",
    "country": "Philippines",
    "role": "Founder, Example Labs",
    "session_pref": "mornings",
    "diet": "Vegetarian",
    "passport_no": "X0000000",
}
COOKIE = "cs_owner"
MCP_VERSION = "2025-06-18"


class RateLimit:
    """At most `n` sandbox-starting requests per address per `window` seconds. A public demo
    must not let one visitor queue every sandbox or spend the model budget."""

    def __init__(self, n: int, window: float):
        self.n, self.window = n, window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(who, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # forget idle addresses
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window][:5_000]:
                    del self._hits[k]
            return True


def _private(rec: dict) -> dict:
    """A run as anyone may see it: never the grant token it ran under."""
    return {k: v for k, v in rec.items() if k != "token"}


def create_app(service: Service | None = None, *, password: str | None = None,
               limit: RateLimit | None = None) -> FastAPI:
    env = os.environ
    limit = limit or RateLimit(int(env.get("CS_RATE_LIMIT", "40")), 600)
    if service is None:
        demo = env.get("CS_DEMO", "1") != "0"
        password = env.get("CS_OWNER_PASSWORD") or None
        if not demo and not password:
            raise SystemExit("CS_DEMO=0 needs CS_OWNER_PASSWORD: a real deployment does not run without an owner password")
        # The spend ledger lives with the app's data, so the cap survives a redeploy.
        env = {**env, "CS_LEDGER": env.get("CS_LEDGER") or str(Path(env.get("CS_DATA", "data")) / "spend.jsonl")}
        try:
            model = Model.from_env(env)
        except ModelError as exc:
            raise SystemExit(f"model configuration is wrong: {exc}")
        origins = frozenset(h.strip().lower() for h in env.get("CS_DEMO_ORIGINS", "form.test").split(",") if h.strip())
        service = Service(env.get("CS_DATA", "data"), model=model, demo=demo, demo_origins=origins,
                          max_jobs=int(env.get("CS_MAX_SANDBOXES", "2")),
                          fixture_container=env.get("CS_FIXTURE_CONTAINER") or None,
                          profile=DEMO_PROFILE if demo else None)
        if env.get("CS_REAP", "1") == "1":
            service.reap_forever()
    secret = hashlib.sha256(service.priv.private_bytes_raw()).digest()  # cookie key, derived, never stored apart

    app = FastAPI(title="Countersign", docs_url="/api/docs", redoc_url=None)
    app.state.service = service
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def session_token() -> str:
        return hmac.new(secret, b"owner", hashlib.sha256).hexdigest()

    def is_owner(request: Request) -> bool:
        if not password:
            return service.demo
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token())

    def owner(request: Request) -> None:
        if not is_owner(request):
            raise HTTPException(401, "sign in as the owner first")

    def metered(request: Request) -> None:
        who = request.client.host if request.client else "?"
        if not limit.allow(who):
            raise Busy("too many runs from your address in the last ten minutes; try again later")

    def bearer(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "send the owner's grant as 'Authorization: Bearer <token>'")
        return auth.split(" ", 1)[1].strip()

    @app.exception_handler(NotFound)
    async def _nf(_, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(Forbidden)
    async def _fb(_, exc):
        return JSONResponse({"detail": str(exc)}, 403)

    @app.exception_handler(Busy)
    async def _busy(_, exc):
        return JSONResponse({"detail": str(exc)}, 429)

    @app.exception_handler(ToolCallError)
    async def _bad(_, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(ValueError)
    async def _val(_, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(run.OutOfScope)
    async def _oos(_, exc):
        return JSONResponse({"detail": f"refused before any container started: {exc}", "refusals": exc.refusals}, 403)

    @app.exception_handler(GrantError)
    async def _ge(_, exc):
        return JSONResponse({"detail": str(exc)}, 401)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "model": service.model.cfg.label if service.model else None}

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        if not password or not hmac.compare_digest(str(body.get("password", "")), password):
            raise HTTPException(401, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, session_token(), httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=12 * 3600)
        return resp

    @app.get("/api/state")
    def state(request: Request):
        me = is_owner(request)
        m = service.model
        return {
            "owner": me, "demo": service.demo, "needs_login": bool(password) and not me,
            "model": m.cfg.label if m else None,
            "spend": {"usd": round(m.spent(), 4), "cap": m.cap_usd} if m else None,
            "profile": service.profile() if me else {},
            "tools": [{"form_id": t.form_id, "name": t.name, "title": t.title, "purpose": t.purpose, "url": t.compiled.url,
                       "params": len(t.params), "boxes": len(t.boxes), "steps": len(t.compiled.steps) + 1,
                       "learned_by": t.learned_by, "learned_at": t.learned_at} for t in service.tools()],
            "grants": service.grants()[::-1] if me else [],
            "runs": [{k: r.get(k) for k in ("id", "kind", "status", "form_id", "caller", "created", "via", "of",
                                            "approved_by", "rejected", "error")}
                     | {"approvable": (r.get("receipt") or {}).get("approvable")} for r in service.runs()] if me else [],
            "fixtures": [f"http://{o}/{n}" for o in sorted(service.demo_origins)
                         for n in ("wizard.html", "plain.html", "hostile.html", "consent.html")] if service.demo else [],
        }

    @app.post("/api/profile")
    async def set_profile(request: Request):
        owner(request)
        return service.set_profile((await request.json()).get("facts") or {})

    @app.post("/api/learn")
    async def learn(request: Request):
        owner(request)
        metered(request)
        body = await request.json()
        rec = service.learn(str(body.get("url") or ""), body.get("form_id") or None)
        return _private(rec)

    @app.get("/api/tools/{form_id}")
    def tool(form_id: str):
        t = service.tool(form_id)
        return {**t.to_dict(), "definition": t.definition(), "name": t.name}

    @app.get("/api/tools/{form_id}/shot/{n}")
    def tool_shot(form_id: str, n: int):
        p = service.tool_shot_path(form_id, n)
        if not p.exists():
            raise HTTPException(404, "no such screenshot")
        return FileResponse(p, media_type="image/png")

    @app.post("/api/grants")
    async def grant(request: Request):
        owner(request)
        b = await request.json()
        return service.issue_grant(caller=b.get("caller", "owner"), form_id=str(b.get("form_id", "")),
                                   facts=list(b.get("facts") or []), max_total=b.get("max_total"),
                                   may_submit=bool(b.get("may_submit")), consent=str(b.get("consent", "accept-terms")),
                                   ttl_minutes=int(b.get("ttl_minutes") or 60))

    @app.post("/api/runs")
    async def start_run(request: Request):
        owner(request)
        metered(request)
        b = await request.json()
        return _private(service.fill_for_owner(str(b.get("form_id", "")), str(b.get("grant_id", ""))))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        owner(request)
        return _private(service.run_record(run_id))

    @app.get("/api/runs/{run_id}/shot/{n}")
    def run_shot(run_id: str, n: int, request: Request):
        owner(request)
        p = service.shot_path(run_id, n)
        if not p.exists():
            raise HTTPException(404, "no such screenshot")
        return FileResponse(p, media_type="image/png")

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, request: Request):
        owner(request)
        metered(request)
        b = await request.json()
        return _private(service.approve(run_id, str(b.get("digest", "")), b.get("consent") or {}))

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, request: Request):
        owner(request)
        return _private(service.reject(run_id))

    @app.get("/api/fixture-log")
    def fixture_log():
        return service.fixture_log()

    # for other agents

    @app.post("/api/tools/{form_id}/call")
    async def call(form_id: str, request: Request, wait: int = 1):
        token = bearer(request)
        metered(request)
        rec = service.call_tool(form_id, await request.json(), token)
        if wait:
            rec = await run_in_threadpool(service.wait, rec["id"])  # never block the event loop
        return _private(rec)

    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            msg = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        if isinstance(msg, list):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches are not supported"}})
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:  # a notification
            return Response(status_code=202)

        def ok(result):
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})

        def err(code, message):
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

        if method == "initialize":
            return ok({"protocolVersion": MCP_VERSION, "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "countersign", "version": "0.2.0"},
                       "instructions": "Each tool fills one learned web form in a sandbox and returns a receipt. "
                                       "Nothing is submitted until the form's owner approves the receipt. "
                                       "Send the owner's grant as 'Authorization: Bearer <token>'."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": [t.definition() for t in service.tools()]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            match = [t for t in service.tools() if t.name == name]
            if not match:
                return err(-32602, f"unknown tool {name!r}")
            try:
                token = bearer(request)
                metered(request)
                started = service.call_tool(match[0].form_id, args, token)
                rec = await run_in_threadpool(service.wait, started["id"])  # never block the event loop
            except HTTPException as exc:
                return _tool_error(mid, exc.detail)
            except (Forbidden, ToolCallError, run.OutOfScope, GrantError, Busy) as exc:
                return _tool_error(mid, f"refused before any container started: {exc}" if isinstance(exc, run.OutOfScope) else str(exc))
            summary = _summary(rec, str(request.base_url))
            return ok({"content": [{"type": "text", "text": json.dumps(summary, indent=1)}], "structuredContent": summary,
                       "isError": rec["status"] != "done"})
        return err(-32601, f"method {method!r} not supported")

    return app


def _tool_error(mid, text: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": True}})


def _summary(rec: dict, base: str) -> dict:
    r = rec.get("receipt") or {}
    return {
        "run": rec["id"], "status": rec["status"], "error": rec.get("error"),
        "approvable": r.get("approvable"), "submitted": False,
        "fields": [{"key": e["key"], "value": e["value"], "status": e["status"]} for e in r.get("entries", [])],
        "blocking": [f"{f['kind']}: {f['detail']}" for f in r.get("flags", []) if f["blocking"]],
        "next": f"The owner approves or rejects this receipt at {base.rstrip('/')}/#run/{rec['id']}. An agent cannot.",
    }


def _make_default():
    return create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("countersign.web.app:_make_default", factory=True, host=os.environ.get("CS_HOST", "0.0.0.0"),
                port=int(os.environ.get("CS_PORT", "8000")), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
