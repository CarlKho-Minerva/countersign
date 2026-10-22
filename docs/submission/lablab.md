# lablab.ai submission: Countersign

DRAFT written by an agent on 2026-10-21 for Carl to edit and post himself. Not his words
until he says so. Nothing here has been submitted. lablab's form limits (from its
submission guide): title at most 50 characters, short description at most 255, long
description at least 100 words, video MP4 under 5 minutes, slides PDF, cover image 16:9.

## Project title (49 characters)

Countersign: forms agents fill, receipts you sign

## Short description (225 characters)

An agent learns a web form once and fills it in a fresh sandbox on Vultr, under a grant
you signed. It stops before Submit and shows you a receipt: every value, known or
guessed. You countersign, and only then does it submit.

## Long description

Agents that fill web forms make mistakes nobody sees. A job title arrives prefilled and
stays wrong, a sponsor-sharing box arrives ticked, the total reads $1,278 where you
expected $0, and the page itself can carry hidden text that tells the agent what to do.

Countersign puts that work in a box and hands you the evidence. You give it a form
address once. A browser in a throwaway Docker sandbox walks the form, pressing Next
through its steps and never Submit, and the model compiles it into a typed tool: a JSON
Schema whose types and choices come from the page itself, plus how each consent box
reads.

Every later call, from you in the web app or from another agent over MCP, carries a
grant you signed: which caller, which form, which facts it may disclose, the largest
total, whether it may submit, and your standing rule for consent boxes (accept terms,
decline marketing and anything optional, and never tick a statement only you can make,
such as work authorisation). The model maps your facts to fields, but it only ever
sees the facts the grant allows, and a value counts as "known" only when code can match
it to one of them. Everything else is labelled a guess, with its reason.

Each fill runs in a fresh container: non-root, read-only, no capabilities, no DNS, no
secrets, capped at 1 CPU, 1 GB and 60 seconds, with a proxy as its only way out that
allows the form's own origin and nothing else. The run stops before Submit and returns a
receipt with a screenshot of each step, every value and its source, every consent box
and what decided it, and every request the sandbox refused. A vision model checks the
screenshots against the receipt as a second witness. It can warn, but it can never
approve or lift a block, because a page can write text aimed at it.

You approve the receipt. A second fresh sandbox replays the fill, must produce the same
receipt you signed, and only then presses Submit; the model reads the confirmation page.

The containment moment in the video: a hostile page hides instructions for the agent and
runs a script that posts every keystroke to another host. The proxy refused that host
21 times, plus the cloud metadata address and the open internet; the collector received
nothing; the instructions are quoted on the receipt, not followed; and the $1,278 total
blocks approval in a way no approval can lift. An agent whose grant names a different
form is refused before any container starts.

Built on Vultr: the control plane (FastAPI web app, grants, receipts, MCP) runs on a
Vultr VM, every model call goes through Vultr Serverless Inference, and every run gets
its own container on that VM, destroyed afterwards. Deploying it is one command.

When it was built: the containment layer (grants, sandbox, proxy, drift check, receipt)
was written on 2026-10-13 with no model in it. The model-driven parts (learning a form,
mapping facts, checking screenshots), the web app, the MCP endpoint, the video and the
slides were built on 2026-10-21, before the November 3 kickoff. The commit dates in the
repository are the real ones. The recorded demo ran on a local Docker host with Azure
OpenAI as the development model; the submitted deployment uses a Vultr VM and Vultr
Serverless Inference.

151 tests pass, 19 of them against real containers, and the end-to-end demo checks 27
claims against the bundled test forms on every run.

## Tracks and tags

- Main track: Blast Radius Zero: Safe Agent Execution on Vultr (pattern B, sandboxed browser use)
- Technologies: Vultr, Vultr Serverless Inference, Docker, Playwright, Chromium, Python,
  FastAPI, Model Context Protocol (MCP), Caddy
- Categories: AI agents, security, automation

## Links (fill in after the Vultr deploy)

- GitHub repository: https://github.com/CarlKho-Minerva/countersign (private until Carl opens it; lablab requires it public)
- Demo application platform: Vultr (VM, Docker)
- Application URL: https://<vm-ip>.sslip.io, printed by `deploy/vultr.sh`
- Video: `countersign-demo.mp4` (3 min 39 s, captions, no voice)
- Slides: `docs/submission/countersign-deck.pdf`
- Cover image: `docs/submission/countersign-cover.png` (1600 x 900)
