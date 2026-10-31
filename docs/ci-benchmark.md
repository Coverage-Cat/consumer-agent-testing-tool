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

## Measured on October 31, 2026

At commit `f93ba57`, on macOS arm64 with Python 3.14.4 and 18 logical CPUs:

| Worker bound | Run 1 | Run 2 | Run 3 | Median |
| --- | ---: | ---: | ---: | ---: |
| 1 | 20.16 s | 19.62 s | 20.14 s | 20.14 s |
| 4 | 6.58 s | 6.08 s | 6.09 s | 6.09 s |

The median ratio was **3.30×**. All 12 cases and their check statuses matched in all six runs;
`model_requests` was zero. The [machine-readable samples](ci-benchmark-v1.json) bind the suite and
implementation digests. These measurements include ordinary development-machine noise and warm-cache
effects; they are not a prediction for another service or CI runner.
