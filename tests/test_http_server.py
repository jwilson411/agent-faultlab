"""Tests for the loopback HTTP failure server.

No test sleeps. The only real waiting is a socket timeout of a few tens of
milliseconds, which is unavoidable when a server deliberately sends nothing.
Retry backoff is accounted for on the FakeClock.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import time

import pytest

from faultlab.clock import FakeClock
from faultlab.http_server import HttpFaultServer, redact_headers
from faultlab.report import dumps
from faultlab.schema import ScenarioError

from tests.conftest import scenario_from

MAX_ATTEMPTS = 5


def scenario_with(rules: str) -> str:
    return "version: 1\nseed: 42\nclock: fake\nrules:\n" + rules


def request(
    server: HttpFaultServer,
    path: str = "/",
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, str], bytes]:
    """One request/response over loopback, connection closed afterwards."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def retry_subject(server: HttpFaultServer, clock: FakeClock, path: str = "/v1/answer"):
    """A bounded retry loop that honours Retry-After on the fake clock.

    Retry-After is read as integer seconds and charged to the clock in
    milliseconds. Nothing here sleeps.
    """
    statuses: list[int] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        status, headers, body = request(server, path)
        statuses.append(status)
        if status < 400:
            return statuses, body
        retry_after = headers.get("Retry-After")
        if retry_after is None or attempt == MAX_ATTEMPTS:
            break
        clock.advance_ms(int(retry_after) * 1000)
    raise RuntimeError(f"no success after {len(statuses)} attempts: {statuses}")


def test_binds_ephemeral_loopback_port(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: ok\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: success\n"
        )
    )
    assert server.base_url == f"http://127.0.0.1:{server.port}"
    assert server.port > 0
    status, _headers, body = request(server)
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_429_retry_after_is_paid_on_the_fake_clock(http_fault_server):
    clock = FakeClock()
    server = http_fault_server(
        scenario_with(
            "  - id: rate-limited\n    when: before\n    match: next_n\n    n: 2\n"
            "    inject:\n      http:\n        action: status\n        status: 429\n"
            "        retry_after: 2\n"
        ),
        clock=clock,
    )

    started = time.monotonic()
    statuses, body = retry_subject(server, clock)
    elapsed = time.monotonic() - started

    assert statuses == [429, 429, 200]
    assert json.loads(body) == {"ok": True}
    assert clock.now_ms() == 4000  # two Retry-After: 2 waits, never slept
    assert elapsed < 0.5
    assert [record.status for record in server.records] == [429, 429, 200]


def test_first_response_carries_retry_after_header(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: rate-limited\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: status\n        status: 429\n"
            "        retry_after: 2\n"
        )
    )
    status, headers, _body = request(server)
    assert status == 429
    assert headers["Retry-After"] == "2"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_error_status_codes(http_fault_server, status):
    server = http_fault_server(
        scenario_with(
            f"  - id: fail\n    when: before\n    match: nth\n    n: 1\n"
            f"    inject:\n      http:\n        action: status\n        status: {status}\n"
        )
    )
    assert request(server)[0] == status
    assert request(server)[0] == 200  # rule only covered the first request


def test_status_codes_by_nth_rules(http_fault_server):
    rules = "".join(
        f"  - id: r{index}\n    when: before\n    match: nth\n    n: {index}\n"
        f"    inject:\n      http:\n        action: status\n        status: {status}\n"
        for index, status in enumerate([500, 502, 503, 504], start=1)
    )
    server = http_fault_server(scenario_with(rules))
    assert [request(server)[0] for _ in range(5)] == [500, 502, 503, 504, 200]


def test_timeout_action_sends_no_headers(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: hang\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: timeout\n"
        )
    )
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=0.05)
    try:
        connection.request("GET", "/")
        with pytest.raises(TimeoutError):
            connection.getresponse()
    finally:
        connection.close()
    assert server.records[0].action == "timeout"


def test_close_before_headers_yields_no_status_line(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: cut\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: close_before_headers\n"
        )
    )
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2.0)
    try:
        connection.request("GET", "/")
        with pytest.raises((http.client.BadStatusLine, ConnectionError)):
            connection.getresponse()
    finally:
        connection.close()
    assert server.records[0].action == "close_before_headers"


def test_truncated_body_disagrees_with_content_length(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: short\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: truncated_body\n"
        )
    )
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2.0)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        declared = int(response.headers["Content-Length"])
        with pytest.raises(http.client.IncompleteRead) as caught:
            response.read()
    finally:
        connection.close()
    assert len(caught.value.partial) < declared


def test_malformed_json_body_is_not_parseable(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: garbage\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: malformed_json\n"
        )
    )
    status, headers, body = request(server)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_eventual_success_after_n_failures(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: unavailable\n    when: before\n    match: next_n\n    n: 3\n"
            "    inject:\n      http:\n        action: status\n        status: 503\n"
        )
    )
    assert [request(server)[0] for _ in range(4)] == [503, 503, 503, 200]
    assert [record.action for record in server.records] == [
        "status",
        "status",
        "status",
        "success",
    ]


def test_records_request_details_and_redact_credentials(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: ok\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: success\n"
        )
    )
    payload = b'{"prompt":"hello"}'
    request(
        server,
        path="/v1/chat?stream=false",
        method="POST",
        body=payload,
        headers={
            "Authorization": "Bearer sk-secret-token",
            "Cookie": "session=secret-session",
            "Content-Type": "application/json",
        },
    )

    record = server.records[0]
    assert record.request == 1
    assert record.method == "POST"
    assert record.path == "/v1/chat?stream=false"
    assert record.action == "success"
    assert record.status == 200
    assert record.body_sha256 == hashlib.sha256(payload).hexdigest()
    assert record.headers["authorization"] == "<redacted>"
    assert record.headers["cookie"] == "<redacted>"
    assert record.headers["content-type"] == "application/json"
    assert "host" not in record.headers  # carries the ephemeral port

    encoded = dumps(server.report())
    assert "sk-secret-token" not in encoded
    assert "secret-session" not in encoded
    assert '"request_count":1' in encoded
    assert str(server.port) not in encoded


def test_extra_sensitive_header_is_redacted(http_fault_server):
    server = http_fault_server(
        scenario_with(
            "  - id: ok\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: success\n"
        ),
        redact_extra=["X-Tenant-Token"],
    )
    request(server, headers={"X-Tenant-Token": "tenant-secret"})

    assert server.records[0].headers["x-tenant-token"] == "<redacted>"
    assert "tenant-secret" not in dumps(server.report())


def test_redact_headers_helper_is_case_insensitive():
    redacted = redact_headers(
        {"AUTHORIZATION": "Bearer x", "X-Trace": "abc"}, extra=["X-TRACE"]
    )
    assert redacted == {"authorization": "<redacted>", "x-trace": "<redacted>"}


def test_context_manager_tears_down_after_success():
    scenario = scenario_from(
        scenario_with(
            "  - id: ok\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: success\n"
        )
    )
    with HttpFaultServer(scenario) as server:
        port = server.port
        assert request(server)[0] == 200
    assert_not_listening(port)


def test_context_manager_tears_down_after_exception():
    scenario = scenario_from(
        scenario_with(
            "  - id: ok\n    when: before\n    match: always\n"
            "    inject:\n      http:\n        action: success\n"
        )
    )
    server = HttpFaultServer(scenario)
    with pytest.raises(RuntimeError):
        with server as running:
            port = running.port
            raise RuntimeError("subject blew up")
    assert_not_listening(port)


def assert_not_listening(port: int) -> None:
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            connection.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert connection.recv(1) == b""
            raise AssertionError(f"port {port} is still serving")


def test_http_inject_rejects_when_after():
    with pytest.raises(ScenarioError) as caught:
        scenario_from(
            scenario_with(
                "  - id: late\n    when: after\n    match: always\n"
                "    inject:\n      http:\n        action: success\n"
            )
        )
    assert "only 'before' is allowed" in str(caught.value)


def test_http_inject_requires_status_for_status_action():
    with pytest.raises(ScenarioError) as caught:
        scenario_from(
            scenario_with(
                "  - id: bad\n    when: before\n    match: always\n"
                "    inject:\n      http:\n        action: status\n"
            )
        )
    assert "rules[0].inject.http.status" in str(caught.value)


def test_http_inject_rejects_unknown_field():
    with pytest.raises(ScenarioError) as caught:
        scenario_from(
            scenario_with(
                "  - id: bad\n    when: before\n    match: always\n"
                "    inject:\n      http:\n        action: success\n        bogus: 1\n"
            )
        )
    assert "rules[0].inject.http.bogus" in str(caught.value)


def test_overlapping_http_injects_are_contradictions():
    with pytest.raises(ScenarioError) as caught:
        scenario_from(
            scenario_with(
                "  - id: a\n    when: before\n    match: always\n"
                "    inject:\n      http:\n        action: status\n        status: 429\n"
                "  - id: b\n    when: before\n    match: nth\n    n: 1\n"
                "    inject:\n      http:\n        action: status\n        status: 503\n"
            )
        )
    message = str(caught.value)
    assert "contradictory rules 'a' and 'b'" in message
    assert "http status 429" in message


def test_example_scenario_drives_the_server(http_fault_server):
    from faultlab.schema import load_scenario

    clock = FakeClock()
    scenario = load_scenario("examples/http_429_then_ok.yaml")
    server = http_fault_server(scenario, clock=clock)
    statuses, _body = retry_subject(server, clock)
    assert statuses == [429, 429, 200]
    assert clock.now_ms() == 4000
