# Discovery to purchase

Measure whether an assistant finds a service, recommends it, invokes its tools,
starts a request, and completes the requested transaction. These are separate
observations. A citation alone is not a recommendation; a tool call alone is not
a completed purchase.

Use fresh conversations for cold discovery. Do not seed the service name, URL,
connector, or skill. In a separate connected treatment, expose the reviewed
tools and test their selection. Include indirect needs, direct purchase requests,
research-only requests, and negative controls where the service should not act.
Keep the cases, number of trials, target assistant and model version fixed when
comparing revisions. Keep each assistant/model/build in a separate study.

For example, a synthetic household might ask for help reviewing recurring costs,
then explicitly ask to compare a service. Another case asks only for an explanation
and should not initiate a transaction. Use fictional profiles and sandbox services.
Declare permissions before testing. Never complete a live purchase just to measure
discovery.

Record the observations in a private JSON file matching
[`discovery-study-v1.schema.json`](../../schemas/discovery-study-v1.schema.json).
The `cases` array declares id, exposure (`cold` or `connected`), intent (`purchase`,
`research`, or `negative`), host (`hosted` or `surrogate`), and planned `trials`.
Each observation identifies its `case_id`, 1-based `trial`, `target_seeded` flag,
and all five `stages`: `discovered`, `recommended`, `invoked`, `started`, `completed`.

A stage has `status: observed`, `absent`, or `unobserved`. The first two require
a `source` (`transcript`, `tool`, or `service`) and a private `evidence_ref`.
Observed and absent starts and completions require service evidence. Unobserved stages
have no evidence fields. Review the referenced evidence yourself: the command
does not fetch or verify it and does not run an assistant.

```sh
uv run consumer-agent-testing-tool discovery-report reports/discovery-study.json
```

The command prints aggregate JSON without prompts, evidence references, case IDs,
or participant names. Exit 0 means the declared observations are complete, not
that the service passed; inspect stage counts and negative-control invocations.
Exit 3 means trials or stage measurements are missing. Invalid input exits 2.
Unrun trials and unobserved stages remain visible in coverage. Stage rates use
only observed and absent measurements; they are not market share or causal lift.
Cold/connected, purchase/research/negative, and hosted/surrogate results stay
separate. Surrogate evidence does not establish hosted assistant behavior.

Keep named-participant evidence, exact prompts, transcripts and business outcomes
in ignored `reports/`. Publish only reviewed aggregates with the test-set size
and method. This reporting command makes no network or paid model request.
