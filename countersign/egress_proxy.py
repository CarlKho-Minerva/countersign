"""The only route out of a sandbox: an allowlisting HTTP proxy, one per run.

Stdlib only. Runs in its own container, attached to the run's internal network and to
the egress network. Every decision is one JSON line on stdout, which the control plane
reads back into the receipt.

A destination must pass twice: its name is on the allowlist, and every address the
name resolves to is outside. The connection then goes to the address that was
checked, so a second DNS answer cannot move it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from urllib.parse import urlsplit

try:
    from .policy import EgressPolicy
except ImportError:  # copied flat into the proxy image
    from policy import EgressPolicy

MAX_HEAD = 16 * 1024
HEAD_TIMEOUT = 10
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 60
MAX_CONNECTIONS = 64


def log(**row) -> None:
    row["t"] = round(time.time(), 3)
    sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def parse_target(method: str, target: str) -> tuple[str, int, str] | None:
    """(host, port, path) for a proxy request line, or None if it is not one."""
    if method == "CONNECT":
        host, sep, port = target.rpartition(":")
        if not sep or not port.isdigit():
            return None
        return host, int(port), ""
    parts = urlsplit(target)
    if parts.scheme != "http" or not parts.hostname or parts.username is not None:
        return None
    try:
        port = parts.port or 80
    except ValueError:
        return None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname, port, path


async def resolve_checked(policy: EgressPolicy, host: str, port: int) -> tuple[str | None, str | None]:
    """(address to connect to, None) or (None, reason)."""
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=1), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        return None, f"{host} did not resolve ({exc.__class__.__name__})"
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return None, f"{host} did not resolve"
    for address in addresses:
        reason = policy.check_resolved(host, address)
        if reason:
            return None, reason
    return addresses[0], None


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def refuse(writer: asyncio.StreamWriter, status: str, reason: str) -> None:
    body = (reason + "\n").encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nX-Countersign: denied\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )
    try:
        await writer.drain()
    except OSError:
        pass
    writer.close()


async def handle(policy: EgressPolicy, gate: asyncio.Semaphore, reader, writer) -> None:
    if gate.locked():
        log(action="deny", reason="too many connections")
        return await refuse(writer, "503 Service Unavailable", "too many connections")
    async with gate:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
            log(action="deny", reason="unreadable request")
            return await refuse(writer, "400 Bad Request", "unreadable request")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ")
        except ValueError:
            log(action="deny", reason="bad request line")
            return await refuse(writer, "400 Bad Request", "bad request line")
        parsed = parse_target(method, target)
        if parsed is None:
            log(action="deny", method=method, target=target[:200], reason="not a proxy request")
            return await refuse(writer, "400 Bad Request", "not a proxy request")
        host, port, path = parsed

        reason = policy.check_host(host, port)
        address = None
        if reason is None:
            address, reason = await resolve_checked(policy, host, port)
        if reason is not None:
            log(action="deny", method=method, host=host, port=port, reason=reason)
            return await refuse(writer, "403 Forbidden", reason)

        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log(action="error", method=method, host=host, port=port, reason=f"connect failed: {exc.__class__.__name__}")
            return await refuse(writer, "502 Bad Gateway", "connect failed")
        log(action="allow", method=method, host=host, port=port, address=address)

        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            # One request per connection, so the next request is checked again.
            kept = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:"))]
            request = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close"]) + "\r\n\r\n"
            up_writer.write(request.encode("latin-1"))
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def main() -> None:
    policy = EgressPolicy.from_env(os.environ)
    gate = asyncio.Semaphore(MAX_CONNECTIONS)
    server = await asyncio.start_server(
        lambda r, w: handle(policy, gate, r, w), "0.0.0.0", int(os.environ.get("CS_PORT", "3128")), limit=MAX_HEAD
    )
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, lambda: stop.done() or stop.set_result(None))
    ttl = float(os.environ.get("CS_TTL", "0"))
    if ttl:  # a proxy never outlives its run, even if the control plane dies
        loop.call_later(ttl, lambda: stop.done() or stop.set_result(None))
    log(action="listening", hosts=sorted(policy.allow_hosts), ports=sorted(policy.allow_ports))
    async with server:
        await stop


if __name__ == "__main__":
    asyncio.run(main())
