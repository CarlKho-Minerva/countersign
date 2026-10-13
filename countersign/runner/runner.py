"""Runs inside the sandbox container. Drives one Chromium page through a fixed set of
verbs, one JSON object per line on stdin, one per line back on stdout.

There is no shell verb and no way to run caller-supplied code. The proxy is the
boundary; the checks here only make a mistake fail earlier and with a clearer error.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ACTION_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 20_000
MAX_NAVIGATIONS = 25
MAX_LINE = 256 * 1024
MAX_SHOT = (1600, 6000)
NEVER_FILL = re.compile(r"(card|cc-|cvv|cvc|iban|routing|account.?number)", re.I)
READ_FORM_JS = (Path(__file__).parent / "read_form.js").read_text()


class Refused(Exception):
    pass


class Runner:
    def __init__(self, page, allow_hosts: set[str]):
        self.page = page
        self.allow_hosts = allow_hosts
        self.fields: dict[str, dict] = {}
        self.navigations = 0
        page.on("framenavigated", self._on_navigate)

    def _on_navigate(self, frame) -> None:
        if frame == self.page.main_frame:
            self.navigations += 1

    def _check_loop(self) -> None:
        if self.navigations > MAX_NAVIGATIONS:
            raise Refused(f"page navigated {self.navigations} times, treating it as a redirect loop")

    def open(self, url: str) -> dict:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in self.allow_hosts:
            raise Refused(f"{url!r} is outside this run's origins")
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightError:
            pass  # a page that never goes idle is still readable
        self._check_loop()
        return {"url": self.page.url, "title": self.page.title(), "status": response.status if response else None}

    def read_form(self) -> dict:
        self._check_loop()
        data = self.page.evaluate(READ_FORM_JS)
        self.fields = {f["key"]: f for f in data["fields"]}
        data["url"] = self.page.url
        return data

    def _locate(self, key: str):
        f = self.fields.get(key)
        if f is None:
            raise Refused(f"no field {key!r} in the last read_form")
        return f, self.page.locator(f'[{f["by"]}="{_css(key)}"]')

    def fill(self, key: str, value: str) -> dict:
        f, loc = self._locate(key)
        if f["type"] in ("checkbox", "password", "file", "hidden", "submit", "button") or NEVER_FILL.search(key) \
                or NEVER_FILL.search(f.get("autocomplete") or ""):
            raise Refused(f"{key} is a consent, secret, payment or control field and is never filled")
        if f["type"] == "select":
            if value not in f["options"]:
                raise Refused(f"{value!r} is not an option of {key}")
            loc.select_option(value=value, timeout=ACTION_TIMEOUT_MS)
        elif f["type"] == "radio":
            if value not in f["options"]:
                raise Refused(f"{value!r} is not an option of {key}")
            self.page.locator(f'[name="{_css(key)}"][value="{_css(value)}"]').check(timeout=ACTION_TIMEOUT_MS)
        else:
            loc.fill(value, timeout=ACTION_TIMEOUT_MS)
        return {"key": key}

    def set_consent(self, key: str, checked: bool) -> dict:
        # Only ever sent with the owner's decision: an approval, or the standing rule in their grant.
        f, loc = self._locate(key)
        if f["type"] != "checkbox":
            raise Refused(f"{key} is not a checkbox")
        loc.set_checked(bool(checked), timeout=ACTION_TIMEOUT_MS)
        return {"key": key}

    def click(self, key: str, submit: bool = False) -> dict:
        f, loc = self._locate(key)
        if f["type"] not in ("submit", "button"):
            raise Refused(f"{key} is not a button")
        if f["type"] == "submit" and not submit:
            raise Refused(f"{key} submits the form; only an approved replay may press it")
        loc.first.click(timeout=ACTION_TIMEOUT_MS)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError:
            pass
        self._check_loop()
        return {"url": self.page.url, "title": self.page.title()}

    def screenshot(self) -> dict:
        # Clipped: a page decides its own height, and must not decide the size of the reply.
        size = self.page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
        clip = {"x": 0, "y": 0, "width": min(size[0], MAX_SHOT[0]), "height": min(size[1], MAX_SHOT[1])}
        png = self.page.screenshot(full_page=True, clip=clip, timeout=ACTION_TIMEOUT_MS)
        return {"png_b64": base64.b64encode(png).decode()}

    VERBS = {"open", "read_form", "fill", "set_consent", "click", "screenshot"}

    def dispatch(self, msg: dict) -> dict:
        verb = msg.get("verb")
        if verb not in self.VERBS:
            raise Refused(f"unknown verb {verb!r}")
        args = {k: v for k, v in msg.items() if k != "verb"}
        return getattr(self, verb)(**args)


def _css(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # nothing but replies may reach the control plane's reader

    def reply(obj: dict) -> None:
        out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        out.flush()

    ttl = int(os.environ.get("CS_TTL", "0"))
    if ttl:  # the sandbox ends itself even if nobody is left to kill it
        signal.alarm(ttl)

    os.makedirs(os.environ.get("HOME", "/tmp/home"), exist_ok=True)
    allow_hosts = {h for h in os.environ.get("CS_ALLOW_HOSTS", "").lower().split(",") if h}
    args = ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    if os.environ.get("CS_CHROMIUM_NO_SANDBOX") == "1":
        args.append("--no-sandbox")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=args,
                chromium_sandbox=os.environ.get("CS_CHROMIUM_NO_SANDBOX") != "1",
                proxy={"server": os.environ["CS_PROXY"], "bypass": "<-loopback>"},
            )
            context = browser.new_context(accept_downloads=False, service_workers="block", viewport={"width": 1280, "height": 900})
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            page = context.new_page()
            page.on("dialog", lambda d: d.dismiss())
            runner = Runner(page, allow_hosts)
            reply({"ok": True, "ready": True, "chromium": browser.version})
            for line in sys.stdin:
                if len(line) > MAX_LINE:
                    reply({"ok": False, "refused": True, "error": "command too large"})
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("verb") == "close":
                        reply({"ok": True})
                        break
                    reply({"ok": True, **runner.dispatch(msg)})
                except Refused as exc:
                    reply({"ok": False, "refused": True, "error": str(exc)})
                except (PlaywrightError, TypeError, ValueError) as exc:
                    reply({"ok": False, "refused": False, "error": f"{exc.__class__.__name__}: {str(exc)[:500]}"})
            browser.close()
    except Exception as exc:  # launch failure: say why on the protocol channel, then fail
        reply({"ok": False, "fatal": True, "error": f"{exc.__class__.__name__}: {str(exc)[:800]}"})
        raise


if __name__ == "__main__":
    main()
