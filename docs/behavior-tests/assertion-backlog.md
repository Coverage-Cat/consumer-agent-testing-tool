# Behavior assertion backlog

Use the [behavior catalog](catalog.yaml) for existing coverage and the
[architecture backlog](../behavior-architecture.md) for shared implementation
work. The September 2026 export describes memory and scheduled work from one
environment. Those source-derived mechanisms have an `unofficial` basis.
Fixture timing, claim promotion, ranking, authorization and acceptance criteria
have an `assumed` basis. [Export account of memory and scheduled work](https://mouse.dev/blog/muse-runtime-export/#memory-and-scheduled-work)

## Local regressions

- The [memory lifecycle tests](../../tests/test_memory_lifecycle.py) stage
  claims from synthetic messages and verify their speaker, ID and supporting
  span. They exercise same-day scope for a comparison instruction, hourly
  promotion and supersession, fresh context, targeted forgetting, reindexing
  and nightly guidance. The fixture author supplies the intended spans and
  scope. A stub captures the input to the fresh conversation. Search and
  explanation are fixture observers.
- The [read-only insurance watch](purchase-flows.md#boundaries-and-failure-controls)
  checks one inbox delivery against four observed polls of a seeded
  application. It reports attempted insurance writes, dispatched requests,
  and committed effects independently. Both supported surrogate targets have
  local coverage.

## Local fixtures to extend

| Priority | Behavior | Acceptance and existing work |
| --- | --- | --- |
| P0 | Claim extraction and recall decisions | Let the driver decide which candidate to write from a user choice, a hypothetical and a connector fact. Verify the supporting source span and one active choice after replay. Probe a fresh chat with tools disabled, then separately permit a live read of a distinct existing policy. Use the state-continuity cases and ARCH-04/07. |
| P1 | Retrieval and evidence | Expose search and explanation to the driver. Seed an experience, a circumstance and a preference with overlapping terms. Require the intended preference in the top three, the right experience for a past-event query, and no claimed evidence for an unsupported query. Check the cited lines against the source. |
| P1 | Account scope and pending jobs | Probe search, explanation and briefing as two accounts with similar contacts. Unlink one source while a job is pending and observe zero fresh reads from it. Delete it in a separate run and verify that its derived item is gone after reindexing. Build on the catalog's state-continuity checks. |
| P1 | Nightly writing style | Compare a new chat with an otherwise identical no-memory control. Count sentences and follow-ups after seeding a preference for one sentence and no unnecessary question. Require the synthesized guidance in the initial input and keep a marker unique to the raw dream out of that input. |
| P1 | Job recovery and receipts | Interrupt hourly and nightly writes at the destination and replay a due slot across restart. Compare each receipt with an independent state read and distinguish a failed write from a no-op. Require zero effects before a job is due and at most one committed change per logical job and input. Extend ARCH-05. |
| P1 | Relationships, recurring workflows, ideas and goal briefings | Correct one of two contacts with the same first name and require the referenced page and later answer to agree. Repeat a read-only review, complete a goal and opt out of a topic. Require a briefing with eligible goals and evidence, zero unrequested external tasks, and no duplicate briefing on replay. |
| P1 | Schedule lifecycle in conversation | Create a daily watch, revise it to weekly and cancel it. Observe one logical task and reads only at revised due times. Require no reads or deliveries after cancellation, including after restart and nightly work. Build on scheduler cancellation and ARCH-03/05. |
| P2 | Skill discovery and routing | Advertise two integrations with different account capabilities. Verify route exposure, the actual invocation and returned reference, and the outcome when the supported capability is unavailable. Extend the catalog's routes cases. |
| P2 | Delegation and channel handoff | Have a browser task observe a changed quote. Verify the parent passes the same reference and price to a simulated messaging or voice delivery adapter, with one delivery. Extend ARCH-03. |
| P2 | Artifact generation and sharing | During a synthetic comparison, open the rendered view and the requested PDF or spreadsheet. Compare both prices and units with service state. Verify that the destination received exactly the named artifact. |
| P2 | Media processing | Generate and decode a thumbnail from a local synthetic video and check its dimensions. Inject unavailability and verify an accurate failure and zero delivered artifacts. |
| P2 | Device permission lifecycle | Pair a mock printer, grant and revoke status access. Observe one authorized read and zero reads before approval or after revocation. Existing gadget registration coverage checks message shape. |

## Hosted observations

The first hosted assertions remain fresh-chat supersession, forgetting through
overnight processing, and a read-only watch with one correct delivery. A
separately authorized study needs an eligible account, observed access to the
relevant surfaces, and independent receipts for live reads, delivery and
insurance effects. Observe wall-clock job windows and retain evidence that the
source and later conversation were both exposed. Hosted routing, channel and
artifact claims need direct observations of the enabled surfaces. Physical
device behavior needs a paired test device and its own authorization.

Use required observation gaps when the trigger, task exposure or observer is
missing. Keep transcripts and unredacted evidence in private study storage.
Record redacted source references, job and claim IDs, statuses and effect
counts only where observed. Follow the
[behavior evidence guidelines](README.md#environment-and-evidence).
