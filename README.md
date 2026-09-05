# agent-faultlab

Deterministic fault injection for a Python callable or a loopback HTTP endpoint, driven by a YAML
scenario.

Retry loops, fallback chains and backoff policies are usually tested with ad hoc mocks that
encode the failure pattern in imperative test code. `agent-faultlab` moves that pattern into a
declarative scenario file, wraps the tool callable, and emits a canonical JSON report of exactly
what happened on each call.

The same scenarios drive a loopback HTTP failure server, so the status codes, headers, malformed
bodies and disconnects that a callable mock cannot express are exercised at the HTTP boundary.

A local idempotency oracle records every synthetic side effect in a SQLite ledger and then answers
whether the run stayed exactly-once, at-most-once and free of key reuse.

Every run is reproducible: the fake clock starts at 0 ms, nothing sleeps, nothing leaves
`127.0.0.1`, and reports contain no wall-clock times, hostnames or ports. The same scenario and
seed produce byte-identical bytes on stdout.

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
faultlab oracle LEDGER [--assert NAME]... [--json-out PATH] [--junit PATH]
faultlab recovery-run SCENARIO [--json-out PATH] [--junit PATH] -- COMMAND [args...]
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
    inject:                 # exactly one of the five kinds below
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
| `http: {action: status, status: 429}` | the response of the HTTP failure server; see below |

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
`delay_fake_clock.yaml`, `http_429_then_ok.yaml`.

## HTTP failure server

Callable mocks cannot show how client code handles status codes, headers, malformed bodies,
disconnects or retry timing, because none of that exists above the HTTP boundary.
`faultlab.HttpFaultServer` is that boundary: an in-process server, bound to `127.0.0.1` on an
ephemeral port, that answers each request with the action of the first matching `http` inject.

```python
from faultlab import FakeClock, HttpFaultServer, load_scenario

clock = FakeClock()
with HttpFaultServer(load_scenario("examples/http_429_then_ok.yaml"), clock=clock) as server:
    call_my_client(server.base_url)   # http://127.0.0.1:<port>
print(server.report())
```

The bind address is always `127.0.0.1`; the server never listens on `0.0.0.0` and never talks to
anything else. Rule matching is the AF-01 matching: `when` / `match` / `n` / `id`, against the
1-based request count. `http` injects are the response itself, so only `when: before` is accepted;
`when: after` is a validation error. Overlapping `http` injects are contradiction-checked like
every other kind. When no rule matches, the response is `200` with the JSON body `{"ok": true}`.

```yaml
inject:
  http:
    action: status          # required
    status: 429             # required when action is 'status', forbidden otherwise
    headers: {X-Trace: abc} # optional, str -> str
    body: "..."             # optional; a string is sent as-is, anything else as JSON
    retry_after: 2          # optional; emitted as the Retry-After header, stringified
```

| action | effect |
| --- | --- |
| `status` | sends the given status code (429, 500, 502, 503, 504, any other) |
| `timeout` | accepts the connection and sends nothing; the client times out |
| `close_before_headers` | accepts, then closes the socket before any status line |
| `truncated_body` | sends a `Content-Length` larger than the bytes written, then closes |
| `malformed_json` | 200 with `Content-Type: application/json` and a body that is not JSON |
| `success` | 200 with a JSON success body |

The server never sleeps. `timeout` withholds the response until the client hangs up or the server
is torn down, and `retry_after` is paid by the subject on the `FakeClock`, so a scenario with two
2-second retries advances the clock 4000 ms while the test finishes in milliseconds of wall time.

`server.records` is a list of `HttpRequestRecord`: request count, method, path with query, headers,
sha256 of the raw request body, the action taken with its status, and fake-clock milliseconds
before and after handling. Bodies are hashed, never stored. Header values for `authorization`,
`cookie`, `set-cookie`, `x-api-key` and `proxy-authorization` are replaced with `<redacted>`
case-insensitively, plus any name passed as `redact_extra=`; redaction happens before a record
exists, so no unredacted copy is kept. The `Host` header is dropped because it only carries the
ephemeral port. `server.report()` adds the seed, the request count and the fake clock, and is
ready for `dumps` — no wall clock, no hostnames, no port.

`tests/conftest.py` provides the `http_fault_server` fixture, a factory that starts a server from
a `Scenario` or an inline YAML string and always stops it, even when the test fails:

```python
def test_gives_up_after_bounded_retries(http_fault_server):
    server = http_fault_server(scenario, clock=clock, redact_extra=["x-tenant-token"])
    ...
```

`stop()` runs `shutdown`, `server_close` and joins the serving thread, and `__exit__` calls it
after success, after a timeout and after an exception in the block.

Out of scope, deliberately: forward proxying, TLS interception, load testing, DNS chaos,
provider-specific API emulation, and any network traffic to anything other than `127.0.0.1`.

## Idempotency oracle

A retry that fires after the side effect already landed is how one message becomes two. Neither a
callable mock nor an HTTP failure server can tell you that happened: the duplicate is only visible
in the side effect. `faultlab.IdempotencyOracle` records every attempt at a synthetic side effect
in a local SQLite ledger and then answers, afterwards, whether the run kept its invariants.

```python
from faultlab import IdempotencyOracle, Ledger, SideEffectSink

with Ledger("run.sqlite") as ledger:
    oracle = IdempotencyOracle(ledger)
    sink = SideEffectSink(name="send_message")
    oracle.execute("send_message", "msg-2f1c", payload, sink.send, call=1, fault="drop")
    ...
    assert oracle.assert_exactly_once() == []
```

`execute` is the idempotent path: it looks the key up first and replays the stored result instead
of running the function again. `effect` always runs the function, which is what a naive retry
does. `fault="drop"` commits and then raises `ResponseLost` instead of returning; `fault="replace"`
commits and hands the caller something else. Both model the response being lost *after* the write
landed.

Each attempt is one ledger row:

| field | meaning |
| --- | --- |
| `operation` | the logical side effect, e.g. `send_message` |
| `idempotency_key` | the caller's key for this unit of work |
| `payload_fingerprint` | sha256 of the canonical JSON payload, so key reuse is detectable |
| `commit_point` | `0` started, `1` committed — the row that says the write landed |
| `result` | the committed result, replayed to a later attempt on the same key |

A separate `commits` table holds the canonical committed row per `(operation, idempotency_key)`
under a UNIQUE constraint, first write wins, so a second commit is detected rather than silently
overwriting.

Four violation kinds:

| kind | what happened |
| --- | --- |
| `duplicate_commit` | the side effect committed twice for one key with the same payload |
| `key_reuse_different_payload` | one key was used for two different payloads |
| `response_lost_after_commit` | the write landed but the caller never got the result |
| `started_never_committed` | an attempt began and never reached the commit point |

`at-most-once` fails on `duplicate_commit`. `no-key-reuse` fails on
`key_reuse_different_payload`. `exactly-once` fails on a `duplicate_commit`, on a
`started_never_committed` for a key with no commit at all, on a `response_lost_after_commit` that
no later attempt recovered, and on an operation named explicitly that recorded no commit at all.
So losing a response and then replaying it under the same key passes exactly-once; re-running the
side effect does not.

### `faultlab oracle`

```
faultlab oracle LEDGER --assert exactly-once --assert at-most-once --assert no-key-reuse --junit out.xml
```

`--assert` is repeatable; with none given all three run. Exit `0` when every assertion passed, `1`
when any violation was found, `2` when the ledger is missing or is not a faultlab ledger (in which
case a human line and a JSON error list go to stderr and stdout stays empty). stdout is one line
of canonical JSON:

```json
{"assertions":["at-most-once"],"ledger":"run.sqlite","ok":false,
 "violations":[{"call":2,"idempotency_key":"msg-2f1c","kind":"duplicate_commit",
   "message":"side effect committed again for a key already committed on call 1",
   "operation":"send_message","payload_fingerprint":"500d5a53..."}]}
```

`--json-out PATH` writes that same text to a file. `--junit PATH` writes a JUnit XML report with
one `<testcase>` per assertion, named exactly `exactly-once`, `at-most-once` and `no-key-reuse`,
each failed one carrying a `<failure>` whose message names the kind, the operation and the call.
The XML has no wall-clock timestamp and no measured duration, so it diffs cleanly and can be
committed as a fixture.

### Naive versus idempotent

`examples/idempotency_subjects.py` has two retry loops around the same side effect, the same key
and the same payload. Both meet the same fault: the first attempt commits and then loses its
response. `naive_retry` retries through `oracle.effect` and sends again; `idempotent_retry`
retries through `oracle.execute` and replays.

```
$ python -m examples.idempotency_demo
{"assertions":["exactly-once","at-most-once","no-key-reuse"],"client":"naive","commits":1,
 "ok":false,"result":{"delivery":2,...},"side_effects":2,
 "violations":[{"call":2,"kind":"duplicate_commit","operation":"send_message",...}]}
{"assertions":["exactly-once","at-most-once","no-key-reuse"],"client":"idempotent","commits":1,
 "ok":true,"result":{"delivery":1,...},"side_effects":1,"violations":[]}
```

Same scenario, same fault: the naive client ran the side effect twice and fails exactly-once and
at-most-once, the idempotent client ran it once and passes all three. Nothing here sleeps
or reads the wall clock; the ledgers land in a temporary directory unless `--ledger DIR` says
otherwise.

## Crash/restart recovery harness

An in-process oracle can only see side effects the process survived to record. A worker that is
killed mid-write and restarted is a different question: does the restart finish the work, or does
it send the message a second time? `faultlab recovery-run` runs the worker as a local subprocess,
kills it at a named point, restarts it against the same state directory, and then asks the
idempotency oracle about the ledger both attempts shared.

```
faultlab recovery-run examples/recovery_crash_before_commit.yaml -- python examples/recovery_worker.py
```

The intentional crash is applied on the first process start only. Every restart runs to `done`, to
`error`, or into a limit, so a passing run is recovery rather than a second lucky crash.

### The worker protocol

The worker writes JSONL to stdout: one JSON object per line, flushed immediately, each with a
string `event` field. stderr is captured and counted toward the byte limit, never parsed. A line
that is not a JSON object with a string `event` is ignored as an event and still counted.

| event | fields | meaning |
| --- | --- | --- |
| `checkpoint` | `name` | a named point the scenario can kill at |
| `side_effect` | `operation`, `idempotency_key`, `payload_fingerprint` | the worker committed a side effect |
| `done` | - | the worker finished its work |
| `error` | `message` | the worker gave up; the run fails with reason `worker_error` |

The harness passes two variables to the worker. `FAULTLAB_STATE_DIR` is the directory that
survives the crash, and `FAULTLAB_LEDGER` is the SQLite ledger path inside it (`ledger.sqlite`).
When `FAULTLAB_STATE_DIR` is already set in the environment, the harness uses that directory and
leaves it alone, which is how you seed it with a damaged file or read it afterwards. When it is
not set, the harness creates a temporary directory and removes it on every exit path.

Reading stops the instant the kill point is parsed, so events the worker managed to write between
the match and the signal landing cannot leak into the report. The child is started in its own
session and the whole process group is signalled, so a worker that forked leaves nothing behind.

### Scenario format

```yaml
version: 1                  # must be 1
kind: recovery              # must be "recovery"; `faultlab validate` still rejects this document
seed: 1                     # required integer; recorded in the report
kill:                       # one of the two styles below, never both
  when: after               # before | after
  checkpoint: pre_commit    # the checkpoint name to kill around
limits:                     # optional; every field has a default
  runtime_ms: 10000         # default 5000
  max_restarts: 1           # default 1
  max_output_bytes: 65536   # default 65536, counted across all attempts
assertions:                 # optional; default exactly-once and at-most-once
  - exactly-once
  - at-most-once
```

The other kill style is by event count: `event: checkpoint | side_effect`, `n:` a positive
integer, and an optional `name:` that narrows the match to one checkpoint name or one operation.
Unknown fields at any depth are errors, and the reported `path` names the key (`limits.budget_ms`,
`kill.whn`). An invalid scenario exits 2 before any command is started.

`runtime_ms` is a real wall-clock cap, not fake-clock milliseconds: it is the ceiling on a worker
that hangs, and it is never recorded in the report. Exceeding a limit is a failure with reason
`limit:runtime` or `limit:output`, and the process group is killed either way.

### The three examples

| scenario | where it kills | what the restart has to do |
| --- | --- | --- |
| `recovery_crash_before_commit.yaml` | after the `pre_commit` checkpoint | decide for itself whether the send already landed |
| `recovery_crash_after_commit.yaml` | after the `commit` checkpoint | read its checkpoint file and skip the send |
| `recovery_corrupt_checkpoint.yaml` | after `begin`, into a damaged state dir | fail loudly instead of sending again |

`examples/recovery_worker.py` keeps a ledger and a checkpoint file in `FAULTLAB_STATE_DIR`, writes
the checkpoint atomically (temp file, fsync, `os.replace`), and sends through
`IdempotencyOracle.execute`. SIGKILL is asynchronous, so in the first scenario the worker may die
before the ledger write, inside it, or just after it; all three land on a restart that commits the
message exactly once.

The third one needs a state directory you prepared:

```
mkdir -p /tmp/faultlab-corrupt
printf 'not json' > /tmp/faultlab-corrupt/checkpoint.json
FAULTLAB_STATE_DIR=/tmp/faultlab-corrupt faultlab recovery-run \
    examples/recovery_corrupt_checkpoint.yaml -- python examples/recovery_worker.py
```

### The report

stdout is one line of canonical JSON, the same encoding as every other report:

```json
{"assertions":["exactly-once","at-most-once"],
 "attempts":[{"attempt":1,"checkpoints":["begin","pre_commit","commit"],"done":false,"error":null,
   "exit_code":null,"killed":true,"limit":null,"side_effects":[{"idempotency_key":"msg-recovery",...}]},
  {"attempt":2,"checkpoints":["begin","commit"],"done":true,"error":null,"exit_code":0,
   "killed":false,"limit":null,"side_effects":[]}],
 "checkpoint_sequence":["begin","pre_commit","commit","begin","commit"],
 "duplicate_side_effects":[],"invariants":{"at-most-once":true,"exactly-once":true},
 "kill_point_reached":true,"ledger_missing":false,"limit":null,"ok":true,"reasons":[],
 "restarts":1,"termination_point":"after:commit","violations":[]}
```

There are no PIDs, no ports, no durations and no wall-clock times in it, so two runs of the same
scenario and command produce byte-identical bytes. The `exit_code` of a killed attempt is `null`
rather than the signal that killed it, for the same reason.

Exit codes: `0` every assertion held and no limit was hit, `1` an invariant failed, a limit was
hit, the kill point was never reached or the worker reported an error, `2` the scenario is invalid
or the command could not be started. `--json-out PATH` writes the same text to a file. `--junit
PATH` writes a JUnit XML report in the suite `faultlab-recovery`, with one `<testcase>` per
assertion plus `kill_point_reached` and `no_duplicate_side_effects`.

Out of scope, deliberately: this is not a production supervisor, not a Kubernetes or systemd
restart policy, and not a workflow engine. It starts one local command, kills it once at a point
you named, restarts it up to `max_restarts` times, and reports. It does not schedule, back off,
health-check, or restart anything that is not the command you passed after `--`.

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
- No framework plugins, no chat UI, no model calls. The only network traffic is loopback, between
  a test and the HTTP failure server it started.
- Not a production transaction manager. The idempotency ledger is a local SQLite file describing
  synthetic side effects inside a test; it does not coordinate, deduplicate or roll back anything
  a real system did.
- Not a process supervisor. The recovery harness kills one local command at one named point and
  restarts it a bounded number of times inside a test; it does not keep anything running.
- Not a payments library, and not advice about handling money. `exactly-once` here is an assertion
  about a recorded test run, not a delivery guarantee about your infrastructure.

## Development

```
make install
make test
```

## License

MIT. Copyright (c) 2026 Justin Wilson.
