"""Oracle behaviour: fingerprints, the four violation kinds, faults, concurrency.

Concurrency here is exercised with ``threading.Barrier`` and asserted on the
ledger only. No test asserts which thread won and no test sleeps.
"""

from __future__ import annotations

import threading

import pytest

from faultlab.idempotency import (
    DUPLICATE_COMMIT,
    KEY_REUSE_DIFFERENT_PAYLOAD,
    NO_COMMIT,
    REPLACED_RESPONSE,
    RESPONSE_LOST_AFTER_COMMIT,
    STARTED_NEVER_COMMITTED,
    IdempotencyOracle,
    KeyReuseError,
    Ledger,
    LedgerError,
    ResponseLost,
    SideEffectSink,
    fingerprint,
)

OPERATION = "send_message"
KEY = "k-1"
PAYLOAD = {"body": "hello", "channel": "#ops"}
OTHER_PAYLOAD = {"body": "goodbye", "channel": "#ops"}


@pytest.fixture
def ledger(tmp_path):
    handle = Ledger(tmp_path / "ledger.sqlite")
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def oracle(ledger):
    return IdempotencyOracle(ledger)


@pytest.fixture
def sink():
    return SideEffectSink(name=OPERATION)


def kinds(violations):
    return [violation["kind"] for violation in violations]


def boom(_payload):
    raise ConnectionError("backend dropped the connection before the commit")


# -- fingerprints ------------------------------------------------------


def test_fingerprint_is_stable_and_key_order_does_not_matter():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint(PAYLOAD) == fingerprint(dict(PAYLOAD))
    assert fingerprint(PAYLOAD) != fingerprint(OTHER_PAYLOAD)
    assert len(fingerprint(PAYLOAD)) == 64


def test_fingerprint_distinguishes_nested_ordering_only_by_value():
    assert fingerprint({"x": [1, 2]}) != fingerprint({"x": [2, 1]})
    assert fingerprint({"x": {"a": 1, "b": 2}}) == fingerprint({"x": {"b": 2, "a": 1}})


# -- started, never committed ------------------------------------------


def test_begin_then_crash_before_commit(oracle):
    with pytest.raises(ConnectionError):
        oracle.execute(OPERATION, KEY, PAYLOAD, boom, call=1)

    violations = oracle.violations()
    assert kinds(violations) == [STARTED_NEVER_COMMITTED]
    assert violations[0]["operation"] == OPERATION
    assert violations[0]["call"] == 1
    assert violations[0]["idempotency_key"] == KEY

    assert oracle.assert_at_most_once() == []
    assert kinds(oracle.assert_exactly_once()) == [STARTED_NEVER_COMMITTED]
    assert oracle.ledger.commits() == []


def test_started_never_committed_does_not_fail_exactly_once_when_a_retry_commits(
    oracle, sink
):
    with pytest.raises(ConnectionError):
        oracle.execute(OPERATION, KEY, PAYLOAD, boom, call=1)
    oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=2)

    assert kinds(oracle.violations()) == [STARTED_NEVER_COMMITTED]
    assert oracle.assert_exactly_once() == []
    assert len(sink) == 1


def test_exactly_once_flags_a_named_operation_with_no_commits(oracle):
    assert kinds(oracle.assert_exactly_once(operation="never_ran")) == [NO_COMMIT]
    assert oracle.assert_exactly_once() == []


# -- duplicate commit --------------------------------------------------


def test_two_commits_same_key_and_payload_is_a_duplicate(oracle, sink):
    oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=2)

    violations = oracle.violations()
    assert kinds(violations) == [DUPLICATE_COMMIT]
    assert violations[0]["call"] == 2
    assert violations[0]["operation"] == OPERATION
    assert violations[0]["payload_fingerprint"] == fingerprint(PAYLOAD)

    assert kinds(oracle.assert_at_most_once()) == [DUPLICATE_COMMIT]
    assert kinds(oracle.assert_exactly_once()) == [DUPLICATE_COMMIT]
    assert oracle.assert_no_key_reuse() == []
    assert len(sink) == 2
    assert len(oracle.ledger.commits()) == 1


def test_first_commit_wins_the_canonical_row(oracle, sink):
    oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=2)

    committed = oracle.ledger.commit_for(OPERATION, KEY)
    assert committed is not None
    assert committed.result["delivery"] == 1
    assert committed.call == 1
    assert committed.commit_point == 1


# -- key reuse ---------------------------------------------------------


def test_same_key_different_payload_through_execute(oracle, sink):
    oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    with pytest.raises(KeyReuseError) as excinfo:
        oracle.execute(OPERATION, KEY, OTHER_PAYLOAD, sink.send, call=2)

    assert excinfo.value.operation == OPERATION
    assert excinfo.value.idempotency_key == KEY

    violations = oracle.assert_no_key_reuse()
    assert kinds(violations) == [KEY_REUSE_DIFFERENT_PAYLOAD]
    assert violations[0]["call"] == 2
    assert violations[0]["payload_fingerprint"] == fingerprint(OTHER_PAYLOAD)
    assert violations[0]["committed_fingerprint"] == fingerprint(PAYLOAD)

    assert len(sink) == 1, "the rejected attempt must not run the side effect"
    assert oracle.assert_at_most_once() == []


def test_same_key_different_payload_through_effect(oracle, sink):
    oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    oracle.effect(OPERATION, KEY, OTHER_PAYLOAD, sink.send, call=2)

    assert kinds(oracle.assert_no_key_reuse()) == [KEY_REUSE_DIFFERENT_PAYLOAD]
    assert oracle.assert_at_most_once() == []
    assert len(sink) == 2
    assert oracle.ledger.commit_for(OPERATION, KEY).payload_fingerprint == fingerprint(
        PAYLOAD
    )


# -- faults ------------------------------------------------------------


def test_drop_fault_commits_then_loses_the_response(oracle, sink):
    with pytest.raises(TimeoutError) as excinfo:
        oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1, fault="drop")

    assert isinstance(excinfo.value, ResponseLost)
    assert str(excinfo.value) == "response dropped after commit"

    violations = oracle.violations()
    assert kinds(violations) == [RESPONSE_LOST_AFTER_COMMIT]
    assert violations[0]["operation"] == OPERATION
    assert violations[0]["call"] == 1

    committed = oracle.ledger.commit_for(OPERATION, KEY)
    assert committed is not None and committed.result["delivery"] == 1
    assert len(sink) == 1
    assert kinds(oracle.assert_exactly_once()) == [RESPONSE_LOST_AFTER_COMMIT]
    assert oracle.assert_at_most_once() == []


def test_replace_fault_returns_a_sentinel_and_keeps_the_real_result(oracle, sink):
    returned = oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1, fault="replace")

    assert returned == REPLACED_RESPONSE
    assert returned is not REPLACED_RESPONSE, "callers must not mutate the sentinel"
    assert oracle.ledger.commit_for(OPERATION, KEY).result == {
        "delivery": 1,
        "sink": OPERATION,
        "payload": PAYLOAD,
    }
    assert kinds(oracle.violations()) == [RESPONSE_LOST_AFTER_COMMIT]


def test_unknown_fault_mode_is_rejected(oracle, sink):
    with pytest.raises(ValueError, match="unknown fault"):
        oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=1, fault="explode")


# -- idempotent replay -------------------------------------------------


def test_execute_replays_after_a_dropped_response(oracle, sink):
    with pytest.raises(ResponseLost):
        oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=1, fault="drop")
    replayed = oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=2)

    assert len(sink) == 1, "the retry must not re-run the side effect"
    assert replayed == oracle.ledger.commit_for(OPERATION, KEY).result
    assert replayed["delivery"] == 1

    assert kinds(oracle.violations()) == [RESPONSE_LOST_AFTER_COMMIT]
    assert oracle.assert_exactly_once() == [], "the retry recovered the lost response"
    assert oracle.assert_at_most_once() == []
    assert oracle.assert_no_key_reuse() == []


def test_exactly_once_still_fails_when_the_lost_response_is_never_recovered(
    oracle, sink
):
    with pytest.raises(ResponseLost):
        oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=1, fault="drop")

    assert kinds(oracle.assert_exactly_once()) == [RESPONSE_LOST_AFTER_COMMIT]


def test_execute_records_the_replay_without_a_second_commit_point(oracle, sink):
    oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=2)

    statuses = [attempt.status for attempt in oracle.ledger.attempts()]
    assert statuses == ["committed", "replayed"]
    assert [a.commit_point for a in oracle.ledger.attempts()] == [1, 0]
    assert oracle.violations() == []
    assert len(sink) == 1


def test_call_ids_are_allocated_when_not_given(oracle, sink):
    oracle.execute(OPERATION, "a", PAYLOAD, sink.send)
    oracle.execute(OPERATION, "b", PAYLOAD, sink.send)

    assert [attempt.call for attempt in oracle.ledger.attempts()] == [1, 2]


# -- ledger ------------------------------------------------------------


def test_open_existing_rejects_a_missing_ledger(tmp_path):
    with pytest.raises(LedgerError, match="ledger not found"):
        Ledger.open_existing(tmp_path / "absent.sqlite")


def test_open_existing_rejects_a_file_that_is_not_a_ledger(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(LedgerError, match="not a faultlab ledger"):
        Ledger.open_existing(path)


def test_a_ledger_survives_being_reopened(tmp_path, sink):
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as first:
        IdempotencyOracle(first).effect(OPERATION, KEY, PAYLOAD, sink.send, call=1)
    with Ledger.open_existing(path) as second:
        assert len(second.commits()) == 1
        assert IdempotencyOracle(second).violations() == []


def test_violations_are_sorted_by_operation_call_kind(oracle, sink):
    oracle.effect("b_op", KEY, PAYLOAD, sink.send, call=2)
    oracle.effect("b_op", KEY, PAYLOAD, sink.send, call=3)
    with pytest.raises(ResponseLost):
        oracle.effect("a_op", KEY, PAYLOAD, sink.send, call=1, fault="drop")

    ordered = [(v["operation"], v["call"], v["kind"]) for v in oracle.violations()]
    assert ordered == [
        ("a_op", 1, RESPONSE_LOST_AFTER_COMMIT),
        ("b_op", 3, DUPLICATE_COMMIT),
    ]


# -- concurrency -------------------------------------------------------


def _race(worker, count=2):
    """Run ``worker(index)`` on ``count`` threads released by one barrier."""
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        try:
            worker(index)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertions
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_concurrent_effect_records_a_duplicate_commit(oracle, sink):
    def worker(index: int) -> None:
        oracle.effect(OPERATION, KEY, PAYLOAD, sink.send, call=index + 1)

    assert _race(worker) == []
    assert len(sink) == 2, "the naive path runs the side effect on both threads"
    assert len(oracle.ledger.commits()) == 1, "only one canonical committed row"
    assert kinds(oracle.violations()) == [DUPLICATE_COMMIT]
    assert kinds(oracle.assert_at_most_once()) == [DUPLICATE_COMMIT]


def test_concurrent_execute_commits_once(oracle, sink):
    def worker(index: int) -> None:
        oracle.execute(OPERATION, KEY, PAYLOAD, sink.send, call=index + 1)

    assert _race(worker) == []
    assert len(sink) == 1, "the idempotent path runs the side effect once"
    assert len(oracle.ledger.commits()) == 1
    assert oracle.violations() == []
    assert oracle.assert_exactly_once() == []

    statuses = sorted(attempt.status for attempt in oracle.ledger.attempts())
    assert statuses == ["committed", "replayed"]


def test_concurrent_execute_with_different_payloads_flags_key_reuse(oracle, sink):
    payloads = (PAYLOAD, OTHER_PAYLOAD)

    def worker(index: int) -> None:
        oracle.execute(OPERATION, KEY, payloads[index], sink.send, call=index + 1)

    errors = _race(worker)
    assert [type(error) for error in errors] == [KeyReuseError]
    assert len(sink) == 1, "the rejected attempt never reaches the side effect"
    assert len(oracle.ledger.commits()) == 1
    assert kinds(oracle.assert_no_key_reuse()) == [KEY_REUSE_DIFFERENT_PAYLOAD]


def test_concurrent_effect_with_different_payloads_flags_key_reuse(oracle, sink):
    payloads = (PAYLOAD, OTHER_PAYLOAD)

    def worker(index: int) -> None:
        oracle.effect(OPERATION, KEY, payloads[index], sink.send, call=index + 1)

    assert _race(worker) == []
    assert len(sink) == 2
    assert len(oracle.ledger.commits()) == 1
    assert kinds(oracle.assert_no_key_reuse()) == [KEY_REUSE_DIFFERENT_PAYLOAD]


def test_concurrent_execute_on_distinct_keys_does_not_interfere(oracle, sink):
    def worker(index: int) -> None:
        oracle.execute(OPERATION, f"k-{index}", PAYLOAD, sink.send, call=index + 1)

    assert _race(worker, count=4) == []
    assert len(sink) == 4
    assert len(oracle.ledger.commits()) == 4
    assert oracle.violations() == []


# -- the demo: naive versus idempotent under the same drop fault --------------


def _demo_ledger_dir(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    return directory


def test_demo_naive_client_duplicates_the_side_effect(tmp_path):
    from examples.idempotency_demo import run_naive

    report = run_naive(str(_demo_ledger_dir(tmp_path) / "naive.sqlite"))

    assert report["client"] == "naive"
    assert report["ok"] is False
    assert report["side_effects"] == 2, "the naive retry ran the side effect again"
    assert report["commits"] == 1, "the second commit never displaced the first"
    assert kinds(report["violations"]) == [DUPLICATE_COMMIT]
    assert report["violations"][0]["operation"] == "send_message"
    assert report["violations"][0]["call"] == 2


def test_demo_idempotent_client_runs_the_side_effect_once(tmp_path):
    from examples.idempotency_demo import run_idempotent

    report = run_idempotent(str(_demo_ledger_dir(tmp_path) / "idempotent.sqlite"))

    assert report["client"] == "idempotent"
    assert report["ok"] is True
    assert report["side_effects"] == 1
    assert report["commits"] == 1
    assert report["violations"] == []


def test_demo_ledgers_satisfy_each_assertion_independently(tmp_path):
    from examples.idempotency_demo import DROP_THEN_RETRY
    from examples.idempotency_subjects import idempotent_retry, naive_retry

    directory = _demo_ledger_dir(tmp_path)
    results = {}
    for name, retry in (("naive", naive_retry), ("idempotent", idempotent_retry)):
        handle = Ledger(directory / f"{name}.sqlite")
        try:
            probe = IdempotencyOracle(handle)
            retry(probe, SideEffectSink(name=OPERATION), faults=DROP_THEN_RETRY)
            results[name] = (
                kinds(probe.assert_exactly_once()),
                kinds(probe.assert_at_most_once()),
                kinds(probe.assert_no_key_reuse()),
            )
        finally:
            handle.close()

    assert results["naive"] == ([DUPLICATE_COMMIT], [DUPLICATE_COMMIT], [])
    assert results["idempotent"] == ([], [], [])


def test_demo_stdout_is_byte_identical_across_runs(tmp_path):
    import io

    from examples.idempotency_demo import main

    def once() -> str:
        buffer = io.StringIO()
        assert main(["--ledger", str(tmp_path / "runs")], stdout=buffer) == 0
        return buffer.getvalue()

    text = once()
    assert once() == text
    lines = text.splitlines()
    assert len(lines) == 2
    assert '"client":"naive"' in lines[0] and '"ok":false' in lines[0]
    assert '"client":"idempotent"' in lines[1] and '"ok":true' in lines[1]
