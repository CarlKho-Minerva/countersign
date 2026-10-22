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

## What was built when

lablab's own FAQ says pre-existing code is allowed and that most events need only the
core AI functionality to be built in the event window. The event's specific rules add
original work, open source and MIT. This entry does not follow the usual split, and says
so plainly:

- **2026-10-13, before kickoff (no model):** sandbox lifecycle and caps, egress proxy,
  grants, fingerprint and drift, receipt format, the consent rule, the hostile form, and
  tests against a real local Docker sandbox.
- **2026-10-21, also before kickoff:** the AI core and everything around it: learning a
  form in steps and compiling it into a typed tool, mapping facts to fields with
  known-or-guess labels, the model's check of each screenshot and of the page after
  Submit, page text aimed at an AI flagged on the receipt, the MCP surface and agent API,
  the web app, the end-to-end demo, the one-command deploy, the video and the slides.

The commit dates are real. The repository stays private until Carl decides to open it.

## As built (2026-10-21)

- **Steps.** Learning presses buttons labelled Next or Continue (never a submit
  control) and records at which step each field first shows. A fill walks the same
  steps, fills each step's fields, screenshots it, and reads the whole form back at
  the end. Text that is merely on a later step is not "hidden text".
- **The typed tool** is a JSON Schema built from the page (types, choices, required)
  with the model's descriptions. It is what `tools/list` returns over MCP.
- **The model is a reader and a witness, never a gate.** Its consent reading can only
  make the rule more careful, except that it may call an oddly worded rules box terms
  when the label names terms, rules or a policy. Its findings on a receipt are "model-"
  flags: they never block, never approve, and are left out of the digest an approval
  signs, because the model is not reproducible call to call and a page can write text
  aimed at it.
- **Data minimisation.** The model sees only the facts a grant allows. A fact outside the
  grant never leaves the control plane.
- **A provider's own filter is evidence.** When Azure's prompt shield refused to read the
  hostile page's hidden text, the witness recorded that as a prompt attack on the
  receipt and checked the screenshots without the page's text.
- **Spend.** Every model call is metered into a ledger and stops, loudly, at a cap. A
  provider that reports no usage is metered at the pre-call estimate.
- **Public demo.** Demo mode learns only the bundled test forms, rate-limits each
  address, queues at most eight runs, and prunes old runs.

Known limits: a form whose Next button posts to its server sends each step's data on
Next; a box whose wording names a policy and also hides a statement the patterns do not
know could still be read as terms by the model (the owner still approves the receipt,
which lists every box and what decided it); the app controls the host's Docker engine.

## Not in scope

Login-walled forms, CAPTCHAs, payments, and anything that needs a stored credential.
Passwords and payment fields are never filled; the receipt lists them as yours. A
caller can never set a consent box: only the owner's rule or the owner's approval can.

The consent rule matches wording with narrow patterns (`countersign/consent.py`). Since
2026-10-21 the model also reads each label when a form is learned; the three outcomes
stay the same, and the patterns run last, on the live label.
