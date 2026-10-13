# Countersign: design

Entry for the Vultr Agent Rush hackathon (lablab.ai, Nov 3 to 8, 2026), main challenge
"Blast Radius Zero", Pattern B (sandboxed browser use). Solo build. Working name.

## The problem

An agent that fills a web form for you makes mistakes you cannot see. A job title
arrives prefilled and stays wrong. Three marketing boxes end up ticked. The total is
$1,278 where you expected $0. The page itself can carry text that tells the agent what
to do. And each run pays for a vision model to rediscover a form it has filled before.

## What it does

1. **Learn once.** Give it a form URL. In a throwaway sandbox, a browser walks the
   form and the model compiles it into a typed tool: the fields, their types and
   options, the steps, the submit control, and a fingerprint of the form's shape.
2. **Call many times.** A person in the web app, or another agent over MCP or HTTP,
   calls that tool with facts. No vision model is needed to replay a form that has
   not changed.
3. **Every call carries a grant.** A signed, expiring token from the owner that says
   who the caller is, which form and which origins it may touch, which facts it may
   disclose, the largest total it may accept, and how consent boxes are decided.
   Anything outside the grant is refused before a container starts.
4. **Every call runs in a fresh sandbox.** One container per run on the Vultr VM: no
   secrets inside, CPU, memory and time caps, a network that can reach only the
   form's own origin through a logging proxy, destroyed when the run ends.
5. **It stops before Submit and returns a receipt.** Each field with its value, where
   the value came from, and whether it is known or a guess. Flags for consent boxes
   that are ticked without an instruction, a total that differs from the expected
   one, page text that tries to instruct the agent, and every network request the
   sandbox blocked.
6. **Consent boxes are decided by your rule, once.** The grant carries a standing
   rule that reads what each box says: accept terms, decline marketing and anything
   optional. A marketing box stays unticked even when the form demands it, and a
   required statement of fact ("I am authorised to work in...") is never the rule's
   to make. Both stop the run and are shown to you as yours to decide. The receipt
   lists every box, what the rule did, and which boxes arrived ticked. A ticked box
   that nobody can see blocks approval.
7. **Drift check.** Before a replay, the live form's fingerprint is compared with the
   compiled one. If the page changed shape, the replay is refused, the difference is
   shown, and the form is learned again.
8. **You countersign.** Approve the receipt and a second fresh sandbox replays the
   same fill and submits. Reject it and nothing left the sandbox.

## The containment moment (for the video)

A hostile form, hosted by us on its own origin:

- Hidden text tells the agent to tick the data-sharing box and send the facts file
  to another domain. The receipt quotes the text and the box stays unticked.
- The page's own script tries to post the typed values to a collector domain. The
  proxy refuses the connection and the receipt lists the blocked request.
- A redirect loop runs until the time cap kills the container.
- A second caller holds a grant for a different form and asks for this one. Refused
  at the boundary, no container started.

## Architecture

```
browser (user)  ──►  control plane on a Vultr VM  ──►  Vultr Serverless Inference
other agents    ──►  (FastAPI, web app, MCP,           (all model calls, key lives
 (MCP / HTTP)         grants, receipts, queue)           only on the control plane)
                             │ narrow verbs over stdio: open, read_form, fill,
                             ▼ select, next, screenshot (never a shell)
              ┌─ per run, on an internal Docker network ─┐
              │  sandbox: Chromium + runner, non-root,   │
              │  read-only root, caps, no secrets        │
              │            │ only route out              │
              │  egress proxy: allowlist from the grant, │──► the form's origin only
              │  logs every allow and deny               │
              └──────────────────────────────────────────┘  destroyed after the run
```

- **Process isolation:** the browser never runs in the app process or on the host.
- **Secret hygiene:** the sandbox gets facts the grant allows and nothing else. No
  API keys, no cookies, no passwords.
- **Resource limits:** memory, CPU, process count and wall time on every run.
- **Lifecycle:** container, proxy and network are removed after each run, pass or fail.

## What is built when

lablab's own FAQ says pre-existing code is allowed and that most events need only the
core AI functionality to be built in the event window. The event's specific rules add
original work, open source and MIT. So:

- **Before kickoff (non-AI scaffolding):** sandbox lifecycle and caps, egress proxy,
  grants, fingerprint and drift, receipt format, the hostile form, tests against a
  real local Docker sandbox.
- **From kickoff, Nov 3 (the AI core, on Vultr):** learning a form with the model,
  mapping facts to fields with known-or-guess labels, vision check of each
  screenshot, injection flagging, MCP surface, web app polish, deploy, video, slides.

The repository goes public at kickoff with its real commit dates, and the README says
which parts predate it.

## Not in scope

Login-walled forms, CAPTCHAs, payments, and anything that needs a stored credential.
Passwords and payment fields are never filled; the receipt lists them as yours. A
caller can never set a consent box: only the owner's rule or the owner's approval can.

Known limit of the consent rule before kickoff: wording is matched with narrow
patterns (`countersign/consent.py`), so an oddly worded terms box is left unticked
and, if required, handed to you. From kickoff the model reads the label; the three
outcomes stay the same.
