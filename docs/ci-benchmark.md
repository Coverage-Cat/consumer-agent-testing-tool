# Scripted CI benchmark

Run from the repository root after `uv sync`:

```sh
uv run python scripts/benchmark_ci.py --jobs 4 --repeats 3
```

The workload is `examples/ci/booking.suite.yaml`: 12 cases covering booking, explicit deferred discovery and
shared readiness under both targets and both native/CLI projections. Each case starts a fresh loopback
service. There is no model inference or external service request.

The script alternates sequential and parallel order over three repetitions. Wall time includes input
validation, service/worker startup, observations, public reporting and process cleanup. It requires every
check outcome and harness identity to match across samples. The JSON records platform, Python, CPU count,
case/implementation digests, per-sample times, medians and their ratio.

Results describe this workload and machine. More workers can increase CPU or I/O contention, and remote
services may impose their own limits. Shared service state requires one worker unless the author provides
case isolation.
