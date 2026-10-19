# Local Glimmer setup and qualification

No model weights are downloaded by `init`, `doctor`, or `run`. The selected
baseline and setup download sizes are recorded in
`src/consumer_agent_testing_tool/data/local-glimmer-baseline.json`. Downloading and verifying those
artifacts is a separate developer setup step.

Configure an exact served model ID, loopback URL, one slot, a 16,384-token context,
and a memory budget appropriate to the actual machine. The weight file's size
does not establish the working-memory requirement.

```yaml
model_profiles:
  local-glimmer:
    provider: local-glimmer
    api: chat-completions
    model_id: your-served-model-id
    base_url: http://127.0.0.1:8080/v1
    context_tokens: 16384
    slots: 1
    memory_budget_mb: 24576
    runtime_pid: 12345
    runtime_build: "copy the pinned server's exact build_info"
    artifacts:
      weights:
        path: ./models/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf
        sha256: "copy the verified weight digest"
      runtime:
        path: ./llama.cpp/llama-server
        sha256: "copy the verified extracted executable digest"
    generation:
      max_output_tokens: 4096
      reasoning_strength: low
    fallback: disabled
```

These illustrative PID/build/hash values cannot qualify a run. `doctor` verifies
the artifact bytes, GGUF architecture and embedded tokenizer/template metadata,
actual process executable, listener ownership, server properties, context and
slot counts. It then exercises native nested tools and result continuation,
checks separate reasoning output, and measures a representative context against
the configured memory budget. A context too small to reserve the configured output
allowance fails. Linux device-memory qualification remains unsupported.

For an offline macOS run, start the server itself under Seatbelt, in addition to
using `--offline` for the coordinator:

```sh
sandbox-exec -p '(version 1)(allow default)(deny network-outbound)' \
  /absolute/path/to/llama-server \
  --model /absolute/path/to/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf \
  --jinja --host 127.0.0.1 --port 8080 --parallel 1 --ctx-size 16384
```

Use the actual server PID and extracted executable digest in the profile.
`runtime_build` must match `/props` exactly. Do not use a template override, inline
reasoning format, speculative drafter, or a second Glimmer output parser for this
baseline. The native server owns prompt rendering and tool parsing.

The offline coordinator verifies the server's kernel default-egress denial.
An unguarded existing endpoint is not an offline-qualified endpoint. Credentials,
model loading and representative-context tests do not establish a full behavioral
release gate; retain the subsequent isolated CLI workflow evidence too.
