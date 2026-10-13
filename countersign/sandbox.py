"""One run, one sandbox: an internal network, an egress proxy, a browser container.

The browser container has no route anywhere except the proxy, no DNS, no capabilities,
a read-only root, and caps on memory, CPU, processes and wall time. All three pieces
are removed when the run ends, whatever the outcome. `reap()` removes anything a
crashed control plane left behind.
"""

from __future__ import annotations

import json
import queue
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .policy import EgressPolicy

LABEL = "countersign.run"
SECCOMP = Path(__file__).resolve().parent.parent / "sandbox" / "seccomp_chromium.json"
PROXY_PORT = 3128
MAX_REPLY = 24 * 1024 * 1024  # one reply line from the runner; a screenshot is the largest


OVERSIZE = object()


class SandboxError(Exception):
    """The sandbox could not be started, spoken to, or cleaned up."""


class SandboxKilled(SandboxError):
    """A cap was hit and the sandbox was destroyed."""


class Refused(Exception):
    """The runner declined a verb. The sandbox is still usable."""


@dataclass(frozen=True)
class Limits:
    memory: str = "1g"
    cpus: str = "1.0"
    pids: int = 256
    shm: str = "256m"
    wall_seconds: int = 90


@dataclass(frozen=True)
class Images:
    box: str = "countersign-box"
    proxy: str = "countersign-proxy"
    egress_network: str = "cs-egress"
    runtime: str | None = None  # "runsc" to run the box under gVisor


def _docker(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"docker {args[0]} did not answer in {timeout}s") from exc
    if check and done.returncode != 0:
        raise SandboxError(f"docker {' '.join(args[:3])} failed: {done.stderr.strip()[:400]}")
    return done


def ensure_egress_network(name: str = "cs-egress") -> None:
    if _docker("network", "inspect", name, check=False).returncode != 0:
        _docker("network", "create", name)


class Sandbox:
    def __init__(self, policy: EgressPolicy, *, limits: Limits = Limits(), images: Images = Images()):
        self.policy, self.limits, self.images = policy, limits, images
        self.run_id = secrets.token_hex(6)
        self.net, self.proxy, self.box = f"cs-{self.run_id}", f"cs-{self.run_id}-proxy", f"cs-{self.run_id}-box"
        self.started = False
        self.killed: str | None = None
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._deadline = 0.0
        self._proxy_rows: list[dict] | None = None
        self._stderr: list[str] = []
        self.chromium: str | None = None

    # lifecycle

    def __enter__(self) -> "Sandbox":
        try:
            self._start()
        except BaseException:
            self.destroy()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.destroy()
        except SandboxError as leak:
            if exc is None:
                raise
            exc.add_note(f"and cleanup failed: {leak}")  # keep the first failure, lose neither

    def _start(self) -> None:
        lim, ttl = self.limits, str(self.limits.wall_seconds + 15)
        label = f"{LABEL}={self.run_id}"
        self.started = True
        self._deadline = time.monotonic() + lim.wall_seconds
        _docker("network", "create", "--internal", "--label", label, self.net)
        _docker(
            "run", "-d", "--name", self.proxy, "--label", label, "--network", self.images.egress_network,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5", "--pids-limit", "64",
            *[x for k, v in {**self.policy.to_env(), "CS_TTL": ttl}.items() for x in ("-e", f"{k}={v}")],
            self.images.proxy,
        )
        _docker("network", "connect", self.net, self.proxy)
        proxy_ip = _docker(
            "inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self.net}").IPAddress}}}}', self.proxy
        ).stdout.strip()
        if not proxy_ip:
            raise SandboxError("proxy has no address on the run network")
        self._wait_for_proxy()

        cmd = [
            "docker", "run", "-i", "--name", self.box, "--label", label, "--network", self.net,
            "--dns", "127.0.0.1",  # the box resolves nothing; the proxy resolves for it
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777", "--shm-size", lim.shm,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--security-opt", f"seccomp={SECCOMP}",
            "--memory", lim.memory, "--memory-swap", lim.memory, "--cpus", lim.cpus, "--pids-limit", str(lim.pids),
            "-e", f"CS_PROXY=http://{proxy_ip}:{PROXY_PORT}", "-e", f"CS_TTL={ttl}",
            "-e", "CS_ALLOW_HOSTS=" + ",".join(sorted(self.policy.allow_hosts)),
        ]
        if self.images.runtime:
            cmd += ["--runtime", self.images.runtime]
        self._proc = subprocess.Popen(
            [*cmd, self.images.box], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        threading.Thread(target=self._pump, args=(self._proc.stdout,), daemon=True).start()
        threading.Thread(target=self._drain, args=(self._proc.stderr,), daemon=True).start()
        ready = self._read(timeout=45)
        if not ready.get("ready"):
            self._raise_if_oom()
            raise SandboxError(f"runner did not start: {ready.get('error', ready)}")
        self.chromium = ready.get("chromium")

    def _wait_for_proxy(self) -> None:
        for _ in range(50):
            if '"action":"listening"' in _docker("logs", self.proxy, check=False).stdout:
                return
            time.sleep(0.1)
        raise SandboxError("egress proxy did not start listening")

    def _pump(self, stream) -> None:
        # Bounded reads: a page must not be able to make the control plane hold an unbounded reply.
        while True:
            line = stream.readline(MAX_REPLY + 1)
            if not line:
                break
            if len(line) > MAX_REPLY:
                self._lines.put(OVERSIZE)
                break
            self._lines.put(line)
        self._lines.put(None)

    def _drain(self, stream) -> None:
        for line in stream:
            self._stderr.append(line)
            del self._stderr[:-50]

    def destroy(self) -> None:
        """Remove everything this run created. Safe to call twice."""
        if not self.started:
            return
        if self._proxy_rows is None:
            self._proxy_rows = self._read_proxy_log()
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        _docker("rm", "-f", "-v", self.box, self.proxy, check=False)
        _docker("network", "rm", self.net, check=False)
        if self._proc:
            self._proc.wait(timeout=10)
        left = _docker("ps", "-aq", "--filter", f"label={LABEL}={self.run_id}").stdout.split()
        nets = _docker("network", "ls", "-q", "--filter", f"label={LABEL}={self.run_id}").stdout.split()
        if left or nets:
            raise SandboxError(f"run {self.run_id} left {len(left)} container(s) and {len(nets)} network(s) behind")
        self.started = False

    # talking to the runner

    def call(self, verb: str, **args) -> dict:
        if self.killed:
            raise SandboxKilled(self.killed)
        if not self._proc or self._proc.poll() is not None:
            raise SandboxError("sandbox is not running" + self._tail())
        try:
            self._proc.stdin.write(json.dumps({"verb": verb, **args}) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise SandboxError("sandbox closed its input" + self._tail()) from exc
        reply = self._read(timeout=self._deadline - time.monotonic())
        if reply.get("ok"):
            return reply
        if reply.get("refused"):
            raise Refused(reply.get("error", "refused"))
        self._raise_if_oom()
        raise SandboxError(reply.get("error", "runner error"))

    def _read(self, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            self._kill(f"time cap of {self.limits.wall_seconds}s reached")
            raise SandboxKilled(self.killed) from None
        if line is OVERSIZE:
            self._kill(f"reply larger than {MAX_REPLY >> 20} MB")
            raise SandboxKilled(self.killed)
        if line is None:
            self._raise_if_oom()
            raise SandboxError(f"sandbox exited (code {self._exit_state().get('ExitCode')})" + self._tail())
        try:
            return json.loads(line)
        except ValueError as exc:
            raise SandboxError(f"runner sent a line that is not JSON: {line[:200]!r}") from exc

    def _raise_if_oom(self) -> None:
        # A crashed browser and a failed launch look alike from the protocol; the kernel knows which.
        if self._exit_state().get("OOMKilled"):
            self._kill(f"memory cap of {self.limits.memory} reached")
            raise SandboxKilled(self.killed)

    def _exit_state(self) -> dict:
        out = _docker("inspect", "-f", "{{json .State}}", self.box, check=False).stdout.strip()
        try:
            return json.loads(out) if out else {}
        except ValueError:
            return {}

    def _kill(self, why: str) -> None:
        self.killed = why
        self.destroy()

    def _tail(self) -> str:
        tail = "".join(self._stderr[-5:]).strip()
        return f": {tail[-400:]}" if tail else ""

    # evidence

    def _read_proxy_log(self) -> list[dict]:
        rows = []
        for line in _docker("logs", self.proxy, check=False).stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"action": "unparsed", "line": line[:200]})
        return rows

    def proxy_log(self) -> list[dict]:
        return self._proxy_rows if self._proxy_rows is not None else self._read_proxy_log()

    def blocked(self) -> list[dict]:
        return [r for r in self.proxy_log() if r.get("action") == "deny"]


def reap(max_age_seconds: int = 600) -> list[str]:
    """Remove sandboxes older than max_age_seconds. Returns what was removed."""
    removed = []
    now = time.time()
    for name in _docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}").stdout.split():
        stamp = _docker("inspect", "-f", "{{.Created}}", name, check=False).stdout.strip()
        if _age(stamp, now) >= max_age_seconds:
            _docker("rm", "-f", "-v", name, check=False)
            removed.append(name)
    for name in _docker("network", "ls", "--filter", f"label={LABEL}", "--format", "{{.Name}}").stdout.split():
        stamp = _docker("network", "inspect", "-f", "{{.Created}}", name, check=False).stdout.strip()
        if _age(stamp, now) >= max_age_seconds and _docker("network", "rm", name, check=False).returncode == 0:
            removed.append(name)
    return removed


def _age(stamp: str, now: float) -> float:
    from datetime import datetime

    try:
        head = stamp.partition(".")[0]
        zone = "+00:00" if stamp.endswith("Z") else stamp[-6:]
        return now - datetime.fromisoformat(head[:19] + zone).timestamp()
    except ValueError:
        return float("inf")  # unreadable age: treat as stale so it cannot linger
