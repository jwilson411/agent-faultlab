# agent-faultlab

Deterministic fault injection for a single Python callable, driven by a YAML scenario.

Retry loops, fallback chains and backoff policies are usually tested with ad hoc mocks that
encode the failure pattern in imperative test code. `agent-faultlab` moves that pattern into a
declarative scenario file, wraps the tool callable, and emits a canonical JSON report of exactly
what happened on each call.

Every run is reproducible: the fake clock starts at 0 ms, nothing sleeps, nothing touches the
network, and the report contains no wall-clock times or hostnames. The same scenario and seed
produce byte-identical bytes on stdout.

## Install

```
git clone https://github.com/jwilson411/agent-faultlab
cd agent-faultlab
pip install -e ".[dev]"
```

Python 3.11+. The only runtime dependency is PyYAML.

## CLI

```
faultlab validate PATH
faultlab run PATH module:function [args...]
```

`validate` loads and checks a scenario and never imports or executes the subject.

```
$ faultlab validate examples/fail_fail_succeed.yaml
{"ok":true,"rules":2,"scenario":"examples/fail_fail_succeed.yaml"}
```

On failure it exits 2 and writes a short human line plus a JSON error list to stderr:

```
$ faultlab validate examples/invalid_contradiction.yaml
faultlab: scenario invalid (1 error); no subject was imported or executed
{"errors":[{"message":"contradictory rules 'always-timeout' and 'second-call-returns': ...","path":"rules"}],"ok":false}
```

`run` validates first, then wraps the scenario's `target.callable` (the tool) and invokes
`module:function` (the subject, typically the retry loop that calls that tool). The tool is
patched by attribute name on its defining module and on the subject's module, then restored when
the run finishes. If the subject reference equals the target, the callable is wrapped and called
directly. If the scenario omits `target`, the subject is also the target.

Exit codes: `0` subject returned, `1` subject raised, `2` scenario invalid or a reference could
not be imported.

## Scenario format

```yaml
version: 1                  # must be 1
seed: 42                    # required integer; recorded in every report
clock: fake                 # required; "fake" is the only accepted value
target:                     # optional; defaults to the CLI subject
  callable: package.mod:func
rules:                      # non-empty; ids must be unique
  - id: r1
    when: before            # before | after, relative to the wrapped call
    match: nth              # nth | next_n | always
    n: 1                    # required for nth and next_n, forbidden for always
    inject:                 # exactly one of the four kinds below
      delay_ms: 250
```

Matchers, against the 1-based call count of the wrapped callable:

- `nth` fires when `call_count == n`
- `next_n` fires on calls `1..n`
- `always` fires on every call

Inject kinds, exactly one per rule:

| kind | effect |
| --- | --- |
| `delay_ms: 250` | advances the fake clock by 250 ms; the real call still happens |
| `exception: {type: TimeoutError, message: "..."}` | raises; `before` skips the real call |
| `return: <value>` | returns the value; `before` skips the real call, `after` replaces the result |
| `malformed: {value: <value>}` | same as `return`, but tagged `malformed` in the report |

Exception types resolve against builtins first, then as `module:Name`.

`when: after` rules run once the real call has returned. If the real call raises, that exception
propagates and no `after` rule fires.

## Validation

Scenarios are rejected before the subject is touched. Unknown fields at any depth are errors and
the reported `path` names the offending key (`rules[0].whn`, `rules[0].inject.bogus`). Also
checked: `version` is 1, `clock` is `fake`, `seed` is an integer, `rules` is non-empty with unique
ids, `when` and `match` are in range, `n` is a positive integer for `nth`/`next_n` and absent for
`always`, `inject` holds exactly one kind, and `delay_ms` is a non-negative number.

Contradictions are rejected too. Two rules that can fire on the same `(when, call-index)` slot
must agree on both kind and value; `always` overlaps every `nth`/`next_n` on the same `when`, and
two `delay_ms` rules on the same slot conflict unless they are equal. The error names both rule
ids. See `examples/invalid_unknown_field.yaml` and `examples/invalid_contradiction.yaml`.

## Example: fail, fail, succeed

`examples/retry_subject.py` has a tool and a retry loop, neither of which sleeps:

```python
def flaky_backend(clock=None):
    return "ok"

def retry_until_ok(clock=None):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return flaky_backend(clock=clock)
        except (TimeoutError, ConnectionError) as exc:
            last_error = exc
            if clock is not None and attempt < MAX_ATTEMPTS:
                clock.advance_ms(BACKOFF_MS * attempt)
    raise RuntimeError(...)
```

`examples/fail_fail_succeed.yaml` fails the first call with `TimeoutError`, the second with
`ConnectionError`, and leaves the third alone:

```
$ faultlab run examples/fail_fail_succeed.yaml examples.retry_subject:retry_until_ok
```

```json
{
  "clock_ms": 300,
  "ok": true,
  "result": "ok",
  "seed": 42,
  "sequence": [
    {"call": 1, "clock_ms_before": 0, "clock_ms_after": 0,
     "injects": [{"id": "first-timeout", "kind": "exception", "type": "TimeoutError", "when": "before"}],
     "raised": {"message": "simulated timeout", "type": "TimeoutError"}, "returned": null},
    {"call": 2, "clock_ms_before": 100, "clock_ms_after": 100,
     "injects": [{"id": "second-connection-reset", "kind": "exception", "type": "ConnectionError", "when": "before"}],
     "raised": {"message": "simulated connection reset", "type": "ConnectionError"}, "returned": null},
    {"call": 3, "clock_ms_before": 300, "clock_ms_after": 300,
     "injects": [], "raised": null, "returned": "ok"}
  ],
  "subject": "examples.retry_subject:retry_until_ok",
  "target": "examples.retry_subject:flaky_backend"
}
```

Exit code 0. The 300 ms on the clock is the subject's own backoff bookkeeping, not elapsed time:
the command returns in milliseconds of wall time, with no sleeps, no network and no model calls.
Actual stdout is one line of canonical JSON
(`json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)` plus a newline), so
reports diff cleanly and can be committed as fixtures.

Other scenarios in `examples/`: `nth.yaml`, `next_n.yaml`, `after_malformed.yaml`,
`delay_fake_clock.yaml`.

## Fake clock

`faultlab.clock.FakeClock` is a millisecond counter starting at 0 that only moves when something
advances it — a `delay_ms` inject, or subject code calling `advance_ms` / `sleep_ms`. The library
never calls `time.sleep` and never reads the wall clock.

The clock is passed as a `clock=` keyword to the subject and to the wrapped tool when their
signatures accept a parameter named `clock`. If they do not, injected delays still advance the
clock and still appear in the report; only the callee cannot observe them.

`examples/delay_fake_clock.yaml` injects `delay_ms: 10000` and returns immediately.

## Library use

```python
from faultlab import load_scenario, run_scenario

scenario = load_scenario("examples/fail_fail_succeed.yaml")
report, exit_code = run_scenario(scenario, "examples.retry_subject:retry_until_ok")
```

`load_scenario` raises `ScenarioError` carrying a list of `ValidationError(path, message)`.

## What this is not

- Not a production chaos tool. It patches a module attribute in the local process; there is no
  agent, no traffic interception and no blast-radius control.
- Not a fuzzer. Faults come only from rules you wrote; nothing is generated or randomized. `seed`
  is recorded so that a future stochastic matcher stays reproducible, but rule matching today is
  fully determined.
- Not a benchmark. Reports carry fake-clock milliseconds, never measured durations.
- No framework plugins, no chat UI, no network calls, no model calls.

## Development

```
make install
make test
```

## License

MIT. Copyright (c) 2026 Justin Wilson.
