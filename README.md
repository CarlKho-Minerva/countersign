# Countersign

Forms your agents can fill, in a sandbox, with a receipt you sign.

An agent learns a web form once and compiles it into a typed tool. Later calls, from a
person in the web app or from another agent over MCP, run in a fresh sandbox under a
signed grant that says who is calling, which origins the sandbox may reach, which facts
it may disclose, the largest total it may accept, and how consent boxes are decided
(your standing rule: accept terms, decline marketing and anything optional, and leave
any statement only you can make to you).

The run stops before Submit and returns a receipt: every field with its value, where
the value came from, and whether it is known or a guess; every consent box and what
decided it; a screenshot of each step; and everything the page tried that the sandbox
refused. You approve the receipt, and only then does a second sandbox replay the fill,
check that it matches what you approved, and submit.

Entry for the Vultr Agent Rush hackathon on lablab.ai (Nov 3 to 8, 2026): main challenge
"Blast Radius Zero", pattern B (sandboxed browser use). Design: [`docs/design.md`](docs/design.md).

## What was built when

Plainly, because the event expects the core AI work inside the event window:

- **2026-10-13, before kickoff:** the containment layer, with no model in it: grants,
  egress proxy, sandbox lifecycle, the runner, drift check, receipt, consent rule,
  countersigned submit, and their tests against real containers.
- **2026-10-21, also before kickoff:** everything model-driven and everything a user
  sees: learning a form (in steps) and compiling it into a typed tool, mapping facts to
  fields with known-or-guessed labels, the model checking screenshots and confirmation
  pages, the web app, the MCP endpoint and agent API, the end-to-end demo, the one-command
  deploy, the deck and the demo video.

Commit dates in this repository are the real ones. Carl Kho built it solo.

## Run it

**Any Linux host with Docker** (the sandboxes need a Linux Docker engine):

```sh
cp .env.example .env      # set the model; see the comments in the file
sh deploy/up.sh           # builds both sandbox images, starts app + test site + Caddy, waits for /healthz
```

Then open `http://<host>/` (or `CS_HTTP_PORT` if you set one). In demo mode (`CS_DEMO=1`,
the default) it learns only its own bundled test forms, which live on a private Docker
network under made-up names, so the public demo cannot be pointed at real sites.

**On a Vultr VM, from your own machine, in one command:**

```sh
VULTR_INFERENCE_API_KEY=... deploy/vultr.sh root@<vm-ip>
```

It installs Docker if missing, copies the checkout, writes `.env` on the VM (mode 600;
the inference key lives only there), runs `deploy/up.sh`, and prints the HTTPS address
(`https://<vm-ip>.sslip.io`). All model calls then go through Vultr Serverless Inference
(`CS_MODEL_PROVIDER=vultr`, default model `qwen3.8-27b`, which accepts images). A 2 vCPU,
4 GB VM is enough for two sandboxes at once.

**The end-to-end demo, from a terminal** (needs Docker and the two images):

```sh
uv sync
sh sandbox/build.sh
CS_MODEL_PROVIDER=... uv run python -m countersign.demo    # or --no-model for rules only
```

It starts the test site itself, runs nine scenes, checks every claim it prints, writes
`runs/demo-*/report.md` with screenshots, and exits 1 if any check fails.

## Tests

```sh
uv run pytest      # 151 tests; the 19 in tests/test_sandbox_real.py need a Linux Docker engine
```

Last full run: 151 passed (132 without containers, 19 against real containers on a Linux
Docker engine). The end-to-end demo with a real model: 27 of 27 checks.

The model-driven code is tested with a scripted model endpoint (`tests/fakes.py`), so the
suite never spends anything.

## How it works

| Step | What happens | File |
|---|---|---|
| Learn | A browser in a throwaway sandbox opens the form, presses Next through its steps (never Submit), and reads every control. The model reads the controls and screenshots and compiles a typed tool. | `countersign/run.py` (`learn_walk`), `countersign/tool.py` |
| Grant | The owner signs a grant (Ed25519): caller, form, origins, facts, largest total, may it submit, consent rule, expiry. | `countersign/grant.py` |
| Map | The model maps the owner's facts to the tool's fields. It only ever sees the facts the grant allows. Code checks every value it calls known. | `countersign/mapper.py` |
| Fill | Refused before a container exists if anything is outside the grant. Otherwise a fresh sandbox fills each step, applies the consent rule, reads everything back, screenshots each step, and stops before Submit. | `countersign/run.py` (`fill`) |
| Receipt | Every field, its source, known or guessed; every box; the total; hidden text quoted; blocked requests. Then the model checks the screenshots as a second witness. | `countersign/receipt.py`, `countersign/witness.py` |
| Countersign | The owner approves the receipt's digest. A second fresh sandbox replays the fill, must produce the same receipt, applies the owner's own consent decisions, and presses Submit. The model reads the page Submit led to. | `countersign/run.py` (`submit`) |

**What the model does, and what it is never trusted with.** It reads forms, maps facts
and checks screenshots. Types, choices and keys always come from the page, so a tool
cannot name a field that does not exist or offer a choice the form does not have. A
value is "known" only if code can match it to a fact. The model's findings on a receipt
never block and never approve (they are left out of the digest an approval signs),
because a page can write text aimed at the model. On consent boxes it can only make the
rule more careful, with one exception: it may read an oddly worded rules box as terms,
and only when the label names terms, rules, a policy or the like. It never ticks
marketing, and never a statement of fact or an authorisation (work authorisation, age,
citizenship, accuracy, payment).

**For other agents.** Every learned form is an MCP tool at `POST /mcp` (`tools/list`,
`tools/call`) and a plain endpoint at `POST /api/tools/<form>/call`. The caller sends the
owner's grant as `Authorization: Bearer <token>` and gets a receipt back. It can never
approve one; only the owner can, in the web app.

## What the sandbox holds

- The browser runs in its own container: non-root, read-only root, every capability
  dropped, Chromium's own sandbox left on, a seccomp profile.
- It has no route out except a per-run proxy, and no DNS. The proxy allows the origins
  named in the grant and refuses addresses, other names, other ports, and names that
  resolve to private or link-local addresses.
- Memory (1 GB), CPU (1), process (256) and wall-time (60 s) caps on every run. Hitting
  one destroys the run and the receipt says which.
- No secrets go in: the box gets the proxy's address and the facts the grant allows.
  The model key and the owner's signing key stay on the control plane.
- Container, proxy and network are removed after every run. A reaper removes anything a
  crash left behind, and old runs are pruned so a public demo cannot fill the disk.

## What it does not stop

- The origin a grant names receives what is typed into its form. That is the point of
  the grant, and the reason it lists the facts by name. A hostile form on an allowed
  origin can read the values it is given; it cannot send them anywhere else.
- A form whose Next button sends data to its server (a server-side multi-page form)
  sends each step's data when Next is pressed. Countersign learns forms whose steps are
  shown and hidden in the page; it never presses a submit control while learning.
- Whoever controls the app controls Docker on its host (the app starts sandboxes through
  the host's engine). Use a strong `CS_OWNER_PASSWORD` outside demo mode.
- Login walls, CAPTCHAs, payments and stored passwords are out of scope. Password and
  payment fields are never filled; the receipt lists them as yours.

## Measured

On a 16-core desktop (Docker in WSL2) with Azure OpenAI `gpt-6.1-sol` as the development model:

| Stage | Time |
|---|---|
| Sandbox start (network, proxy, browser) | 1.4 to 1.8 s |
| Fill a two-step form in the sandbox | about 1.5 s |
| Model: learn a form / map facts / check screenshots | 5 to 7 s / 3 to 5 s / 4 to 6 s |
| Whole fill, from click to receipt | 9 to 13 s |
| Countersigned submit, including the confirmation check | about 5 s |

One full demo run (nine scenes, nine model calls) cost $0.057 and took 108 s. The control
plane process peaked at 36 MB; each sandbox is capped at 1 GB.

## Files

| Piece | File |
|---|---|
| Signed, scoped, expiring grants | `countersign/grant.py` |
| Egress policy and proxy | `countersign/policy.py`, `countersign/egress_proxy.py` |
| Sandbox lifecycle: start, caps, kill, destroy, reap | `countersign/sandbox.py` |
| The runner inside the sandbox: fixed verbs, no shell | `countersign/runner/` |
| Form fingerprint and drift check | `countersign/fingerprint.py` |
| Consent rule | `countersign/consent.py` |
| Receipt | `countersign/receipt.py` |
| Learn, fill, countersigned submit | `countersign/run.py` |
| Model client, spend meter and cap | `countersign/model.py` |
| Typed tool | `countersign/tool.py` |
| Facts to fields | `countersign/mapper.py` |
| Screenshot and confirmation checks | `countersign/witness.py` |
| Web app, agent API, MCP | `countersign/web/` |
| End-to-end demo | `countersign/demo.py` |
| Deploy | `Dockerfile`, `compose.yaml`, `deploy/` |
| Walkthrough, video and deck | `scripts/walkthrough.py`, `scripts/make_media.py` |
| Submission text | `docs/submission/` |

## Licence

MIT. `sandbox/seccomp_chromium.json` is from Playwright (Apache 2.0); see `sandbox/NOTICE`.
