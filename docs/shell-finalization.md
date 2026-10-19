# Final CLI snapshots

An oracle snapshot is not a final outcome while evaluated processes can still
change the service. A reproduced race returned zero drafts and passed all
checks, while a detached child then created a draft before container cleanup.
The reproduction used the actual local Docker boundary and a scripted Inspect
model against the owned fixture.

The CLI runner now finalizes execution before either its normal scorer snapshot
or its interrupted-execution snapshot:

1. Seal the agent execution interface against further calls.
2. Kill the evaluated container, including detached children, and verify its
   stopped state through trusted Docker control.
3. Ask the private service controls to close public ingress and drain accepted
   requests. Stopping a client does not undo a request already accepted by the
   service.
4. Read the independent backend and canary snapshots.

The built-in fixture closes its HTTP/canary listeners and joins accepted request
handlers. Socket reads/writes have bounded timeouts. Private snapshots remain
available; reset cannot reopen the quiesced fixture. Concurrent and repeated
finalization calls are serialized and idempotent.

`execution.quiesced` is a required check. `execution_quiescence` records the
confirmation or an explicit observation gap; the run lock records the
finalization contract. An unavailable, malformed or wrong-trial confirmation
cannot turn zero observed effects into a passing run. The runner still captures
available snapshots after a finalization failure: a positive forbidden effect
remains a failure. Exports preserve both observations and finalization status.

Developer CLI services must implement the private `quiesce` operation described
in [the CLI contract](custom-cli.md). Its acknowledgement is an assertion by the
developer-controlled oracle. The service must not acknowledge while background
or remote work can still change the asserted outcome; unsupported finality stays
unverified. The host does not infer external-service finality from a closed
client connection.

This is a final-state measurement boundary. It does not establish live-model
reliability, broader browser isolation, or real-Muse behavior.
