# Countersign

Forms your agents can fill, in a sandbox, with a receipt you sign.

An agent learns a web form once and compiles it into a typed tool. Later calls, from a
person or from another agent, run in a fresh sandbox under a signed grant that says
who is calling, which origins the sandbox may reach, and which facts it may disclose.
The run stops before Submit and returns a receipt: every field with its value, where
the value came from, and whether it is known or a guess, plus what the page tried that
the sandbox refused. You approve the receipt, and only then does a second sandbox
replay the fill and submit.

Design: [`docs/design.md`](docs/design.md).

## Status

Built for the Vultr Agent Rush hackathon on lablab.ai (Nov 3 to 8, 2026). The
containment layer below was written before kickoff, as scaffolding with no model in
it. The model-driven parts (learning a form, mapping facts to fields, checking
screenshots) and the web app are built during the event, on Vultr.

| Piece | File |
|---|---|
| Signed, scoped, expiring grants | `countersign/grant.py` |
| Egress policy and proxy (the sandbox's only route out) | `countersign/policy.py`, `countersign/egress_proxy.py` |
| Sandbox lifecycle: start, caps, kill, destroy, reap | `countersign/sandbox.py` |
| The runner inside the sandbox: six verbs, no shell | `countersign/runner/` |
| Form fingerprint and drift check | `countersign/fingerprint.py` |
| Receipt | `countersign/receipt.py` |
| Learn, fill, countersigned submit | `countersign/run.py` |

## What the sandbox holds

- The browser runs in its own container: non-root, read-only root, every capability
  dropped, Chromium's own sandbox left on.
- It has no route out except the proxy, and no DNS. The proxy allows the origins named
  in the grant and refuses addresses, other names, other ports, and names that resolve
  to private or link-local addresses.
- Memory, CPU, process and wall-time caps on every run. Hitting one destroys the run
  and the receipt says which.
- No secrets go in: the box gets the proxy's address and the facts the grant allows.
- Container, proxy and network are removed after every run. `reap()` removes anything
  a crash left behind.

## What it does not stop

The origin a grant names receives what is typed into its form. That is the point of
the grant, and the reason it lists the facts by name. A hostile form on an allowed
origin can read the values it is given; it cannot send them anywhere else.

## Run the tests

```sh
uv sync
sh sandbox/build.sh        # builds countersign-box and countersign-proxy
uv run pytest              # the container tests need a Linux Docker engine
```

The container tests start a small site on a private Docker network: a plain
registration form, the same form with a changed shape, a hostile copy that tries to
send typed values to a second host, and a page that hangs.

## Licence

MIT. `sandbox/seccomp_chromium.json` is from Playwright (Apache 2.0); see `sandbox/NOTICE`.
