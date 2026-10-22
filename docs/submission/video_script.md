# Demo video script: Countersign

DRAFT written by an agent on 2026-10-21. The recorded video uses these captions and no
voice. If Carl records a voice-over, the right-hand column is a starting point in plain
words, not his words.

- File: `countersign-demo.mp4`, 1600 x 900, H.264, 3 min 39 s (lablab's limit is 5 min).
- Recorded by `scripts/walkthrough.py --video` driving the real web app: every learn,
  fill and submit in it ran for real, in real containers, with a real model (Azure
  OpenAI gpt-6.1-sol, the development model). Nothing is mocked or sped up.
- Built by `scripts/make_media.py`: a 5 s title card, the recording, an 8 s closing card.
- To re-record against the Vultr deployment: run the walkthrough against its URL with
  `--video`, then `make_media.py`. Both are one command each (see the README).

## The containment moment

From 1:52: a hostile page hides instructions for the agent and posts every
keystroke to collector.test. The proxy refuses every attempt, the receipt quotes the
instructions instead of following them, the $1,278 total blocks approval, and the bar at
the bottom shows the collector received 0 requests. Then an agent holding a grant for a
different form is refused before any container starts.

## Shot list (time in the MP4, caption on screen)

| Time | Caption |
|---|---|
| 0:00 | Title card: Countersign. Forms your agents can fill, in a sandbox, with a receipt you sign. |
| 0:05 | Countersign: forms your agents can fill, in a sandbox, with a receipt you sign. |
| 0:09 | Step 1: learn a form once. This one has two steps, an oddly worded rules box and a pre-ticked newsletter. |
| 0:13 | A browser in a throwaway sandbox walks the form, pressing Next but never Submit. Only form.test is reachable. |
| 0:17 | The model reads the controls and screenshots. Types and choices come from the page, never from the model. |
| 0:24 | The form is now a typed tool: parameters, steps, and how each consent box reads. |
| 0:28 | The model read the rules box as terms. The newsletter is marketing, so your rule leaves it unticked. |
| 0:34 | Another agent sees this JSON Schema over MCP. No vision model is needed to replay the form. |
| 0:38 | Step 2: grant access. A signed, expiring grant: which facts, the largest total, may it submit, the consent rule. |
| 0:43 | Ada Example is a made-up person. Her passport number stays out of the grant, so the model never sees it. |
| 0:48 | Signed with the owner's key (Ed25519). Anything outside it is refused before a container starts. |
| 0:52 | Step 3: fill. The model maps Ada's allowed facts to fields, then a fresh sandbox fills both steps and stops before Submit. |
| 1:06 | The receipt. Every field, where its value came from, and whether it is known or a guess. |
| 1:11 | 'Philippines' became the option PH: still a known fact. Organisation and session are labelled guesses, with reasons. |
| 1:17 | The model checks each screenshot against the receipt. A witness, never a gate: it cannot approve or unblock. |
| 1:22 | A screenshot of each step, taken inside the sandbox. |
| 1:26 | The sandbox's only way out is a proxy that allows the form's own origin. Every request is logged. |
| 1:31 | Step 4: countersign. Approving signs this exact receipt. |
| 1:34 | A second fresh sandbox replays the fill. If it does not match the receipt you signed, nothing is submitted. |
| 1:43 | Submitted once. The model read the confirmation page. The bar below counts what the test site actually received. |
| 1:49 | Before and after Submit, from inside the second sandbox. Both sandboxes are already destroyed. |
| 1:52 | Now the containment moment: a hostile page. |
| 1:55 | Hidden text orders the agent to tick boxes and leak data; its script posts whatever is typed to collector.test. |
| 2:07 | Same flow, fresh sandbox. The page attacks while the fields are typed. |
| 2:28 | Blocked: the page asks for $1,278 and the grant allows $0. That block cannot be approved away. |
| 2:33 | The hidden instructions are quoted as evidence, not followed. The sponsor box stays unticked. |
| 2:39 | The proxy refused every attempt to reach collector.test, the cloud metadata address and the open internet. |
| 2:43 | The collector received nothing: see the bar at the bottom. |
| 2:46 | Other agents use the same tools over MCP, with a grant. Here an agent holds a grant for the workshop form only. |
| 2:52 | It asks for the hostile form instead. Refused at the boundary: no container was ever started. |
| 3:07 | Last: a form that demands a work-authorisation statement. |
| 3:18 | Terms accepted, partner offers declined, and the statement is yours alone. Countersign never ticks it for you. |
| 3:23 | Every run, refusal and submission is on the record. Built for Vultr: VM control plane, Serverless Inference, a container per run. |
| 3:31 | Closing card: what you just saw; recorded on a local Docker host with a development model; production is a Vultr VM with Vultr Serverless Inference; the model-driven parts were built on 2026-10-21, before the Nov 3 kickoff. |
