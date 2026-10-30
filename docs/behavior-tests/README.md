# Sandbox behavior tests

These tests examine observable behavior against fictional services in a
controlled environment. They are organized by behavior, independently of the
assistant or model used. The [catalog](catalog.yaml) links each family to
existing local checks and implemented opt-in capabilities.

Sandbox exercises have covered scoped actions, changing terms, delayed replies,
interrupted workflows, retained state, recovery from service errors, credential
handling, route selection and outcome reporting. They used isolated service
state and synthetic transactions. Some exercises included controlled
communication accounts. No real purchase or funded payment was required.

These exercises informed the [architecture recommendations](../behavior-architecture.md).
They do not constitute a benchmark, a claim that every check passed, or proof
of an assistant's private implementation. A blocked capability, an unexposed
condition and a hosting interruption remain distinct from a behavioral failure.

## Test families

| Family | What the sandbox exercises | What determines the result |
| --- | --- | --- |
| Authorization | Requested reads and writes, denied actions, changed terms | Actual dispatch and committed state under the applicable grant |
| Interruption | New instructions arriving during an ongoing workflow | Delivery evidence, acknowledgment and actions after the stop boundary |
| State continuity | Returning to an authenticated service or earlier preference | Service/session observations, with browser state and conversation memory kept distinct |
| Recovery | Lost responses, expired sessions and retry instructions | Independent state reads, request sequence and duplicate effects |
| Outcome reporting | Pending, completed, failed and quoted outcomes | Agreement between the final answer and authoritative service state |
| Background work | Delayed permission, bounded watches and action limits | Observed reads/actions within a valid observation window |
| Content trust | Instructions embedded in service content and look-alike pages | Unrequested actions or disclosure to an unintended destination |
| Credentials | Synthetic codes, sign-in links and owner-requested summaries | Separate records of use, disclosure and destination |
| Routes | Website, protocol and controlled contact alternatives | Which alternatives were exposed and which route was actually used |
| Checkout | Optional extras, storage choices and changed totals | Submitted options and the committed fictional transaction |
| Presentation | Comparisons, long text and multipart replies | Requested content against the captured delivery surface |
| Capability boundaries | Unavailable transports or account connections | Attempted operations and explicit blockers; never inferred success |
| Harness validity | Availability, message delivery and viable fixture alternatives | Independent controls showing the intended condition could be observed |

The catalog is a design and navigation aid, not a runnable scenario schema or
a record of passes. Existing regression references show related coverage.
The [runtime guide](../behavior-runtime.md) states each capability's scope and
limits. Local component checks do not add observations of an external subject.

## Environment and evidence

Local regression checks use loopback listeners, fake accounts and scripted
inputs or stub models. They make no paid inference request. A separately
authorized external exercise can expose a dedicated fixture endpoint while
retaining its controls locally.

Here, *sandbox* means isolated test data, constrained service actions and
operator-controlled resources. It does not claim that an externally hosted
assistant runs inside a CATT operating-system sandbox. The assistant's own
infrastructure remains outside the observer's view.

Each exercise should record:

- The case, fixture revision, input surface and prior-context conditions.
- Which prerequisite and trigger were actually exposed.
- Requests and independent service state, including zero-effect outcomes.
- Relevant message delivery, scope changes and observation times.
- Infrastructure gaps and a qualified result, separate from task completion.

Use fictional accounts and a backend that records actual effects. A failed
response can follow a committed write; a successful-looking receipt can
disagree with the authoritative state. Neither the response text nor the
assistant's explanation substitutes for that state.

Before testing a prohibition or action limit, demonstrate in a separate control
that the fixture could perform the prohibited extra action. Before comparing
routes, record that the alternatives were presented. Before interpreting a long
silence, record public reachability independently of the local process.

## Reusing local checks

Start with the [connector workflow](../connector-lane.md) and
[existing scenarios](../../examples/connector/README.md). Use the scripted
driver for deterministic comparisons. Choose any supported surrogate profile
as an explicit treatment and retain it in the run evidence; a neutral public
case name must not erase the actual profile from private records.

The catalog's `local_regressions` paths are existing pytest files. For example:

```sh
uv run pytest -q -p no:cacheprovider \
  tests/test_answer_claims.py \
  tests/test_ext_scheduler_config.py \
  tests/test_ext_routes_trace.py
```

These checks validate CATT components. They do not replay an external
assistant's behavior or certify compatibility with it.

New fixtures should use the same behavior family names. Keep exact participant
prompts, endpoint addresses, native transcripts, provider records and
subject-specific conclusions in the ignored `reports/` directory. Publish
general methods and carefully qualified coverage descriptions. Do not publish
a named-assistant scorecard from these exploratory exercises.

Historical study invitations do not authorize another run. Further external
testing requires a new bounded scope; the completed exploratory campaign is
closed.
