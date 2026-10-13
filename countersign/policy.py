"""Which destinations a sandbox may reach. Used by the egress proxy; stdlib only,
because this file is copied into the proxy image as is.

Two checks, both must pass: the name asked for is on the allowlist, and the address it
resolves to is not inside (loopback, private, link-local, the metadata service). The
second check is what stops an allowed name from being pointed at the host.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_NUMERIC = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$")
_NEVER = ipaddress.ip_network("169.254.0.0/16")


def normalise(host: str) -> str:
    return host.strip().lower().rstrip(".")


@dataclass(frozen=True)
class EgressPolicy:
    allow_hosts: frozenset[str]
    allow_ports: frozenset[int] = frozenset({80, 443})
    # Names allowed to resolve to private addresses. Only for fixtures on a test network.
    private_ok: frozenset[str] = field(default_factory=frozenset)

    def check_host(self, host: str, port: int) -> str | None:
        """None if allowed, else the reason for the refusal."""
        name = normalise(host)
        # Chromium accepts 2130706433 and 0x7f.1 as addresses, so refuse anything numeric.
        if not _HOSTNAME.match(name) or _NUMERIC.match(name):
            return f"{host!r} is not a hostname (addresses are never allowed)"
        if name not in {normalise(h) for h in self.allow_hosts}:
            return f"{name} is not in allowlist"
        if port not in self.allow_ports:
            return f"port {port} is not allowed"
        return None

    def check_resolved(self, host: str, address: str) -> str | None:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip in _NEVER:
            return f"{host} resolves to {ip}, a private link-local address"
        if ip.is_global:
            return None
        if normalise(host) in {normalise(h) for h in self.private_ok}:
            return None
        return f"{host} resolves to {ip}, a private address"

    def to_env(self) -> dict[str, str]:
        return {
            "CS_ALLOW_HOSTS": ",".join(sorted(self.allow_hosts)),
            "CS_ALLOW_PORTS": ",".join(str(p) for p in sorted(self.allow_ports)),
            "CS_PRIVATE_OK": ",".join(sorted(self.private_ok)),
        }

    @classmethod
    def from_env(cls, env) -> "EgressPolicy":
        def split(key):
            return frozenset(x for x in env.get(key, "").split(",") if x)

        return cls(
            allow_hosts=split("CS_ALLOW_HOSTS"),
            allow_ports=frozenset(int(p) for p in split("CS_ALLOW_PORTS")) or frozenset({80, 443}),
            private_ok=split("CS_PRIVATE_OK"),
        )
