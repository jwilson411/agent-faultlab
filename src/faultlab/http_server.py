"""In-process HTTP failure server driven by a scenario.

The server binds 127.0.0.1 on an ephemeral port and answers every request with
the action of the first matching ``http`` inject, or a 200 JSON success when no
rule matches. It never sleeps and never reads the wall clock: timings in records
come from the injected :class:`~faultlab.clock.FakeClock`.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Mapping, Sequence

from .clock import FakeClock
from .report import jsonable
from .schema import Inject, Scenario

LOOPBACK = "127.0.0.1"
REDACTED = "<redacted>"
DEFAULT_REDACT = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
)
# The Host header carries the ephemeral port, which would make reports differ
# run to run; it holds no signal for a loopback server, so it is not recorded.
DROPPED_HEADERS = ("host",)
DEFAULT_SUCCESS_BODY = {"ok": True}
MALFORMED_JSON_BODY = '{"ok": true,'
TRUNCATION_PAD = 16
# Poll interval for a withheld ("timeout") response; the handler is waiting for
# the client to hang up or for teardown, not simulating elapsed time.
POLL_SECONDS = 0.05


def redact_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    extra: Sequence[str] | None = None,
) -> dict[str, str]:
    """Lowercase header names, replacing sensitive values with ``<redacted>``."""
    sensitive = {name.lower() for name in DEFAULT_REDACT}
    sensitive.update(name.lower() for name in (extra or ()))
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for name, value in items:
        key = str(name).lower()
        if key in DROPPED_HEADERS:
            continue
        out[key] = REDACTED if key in sensitive else str(value)
    return dict(sorted(out.items()))


@dataclass
class HttpRequestRecord:
    """One handled request. Header values are already redacted."""

    request: int
    method: str
    path: str
    headers: dict[str, str]
    body_sha256: str
    action: str
    clock_ms_before: int
    clock_ms_after: int
    status: int | None = None
    rule: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "body_sha256": self.body_sha256,
            "clock_ms_after": self.clock_ms_after,
            "clock_ms_before": self.clock_ms_before,
            "headers": dict(self.headers),
            "method": self.method,
            "path": self.path,
            "request": self.request,
            "rule": self.rule,
            "status": self.status,
        }


@dataclass(frozen=True)
class _Plan:
    """What to do with one request, decided before any bytes are written."""

    action: str
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    body: Any = None
    rule: str | None = None


class _FaultHandler(BaseHTTPRequestHandler):
    server_version = "faultlab"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Silence the stderr access log; records are the output."""

    def handle_one_request(self) -> None:
        """Parse one request and answer it with raw bytes.

        Nothing is written until the plan is known, which is what makes the
        header-less actions (``timeout``, ``close_before_headers``) possible.
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline or len(self.raw_requestline) > 65536:
                return
            if not self.parse_request():
                return
            self._respond()
        except OSError:
            return
        finally:
            self.close_connection = True

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _respond(self) -> None:
        controller: HttpFaultServer = self.server.controller  # type: ignore[attr-defined]
        body = self._read_body()
        plan, record = controller.begin(
            method=self.command,
            path=self.path,
            headers=self.headers.items(),
            body=body,
        )
        try:
            if plan.action == "timeout":
                self._withhold()
            elif plan.action == "close_before_headers":
                self._close_now()
            else:
                self._write_response(plan)
        finally:
            controller.finish(record)

    def _withhold(self) -> None:
        """Accept the request and never answer it, without sleeping on a timer."""
        stopping: threading.Event = self.server.stopping  # type: ignore[attr-defined]
        connection = self.connection
        connection.settimeout(POLL_SECONDS)
        while not stopping.is_set():
            try:
                if not connection.recv(4096):
                    return
            except TimeoutError:
                continue
            except OSError:
                return

    def _close_now(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def _write_response(self, plan: _Plan) -> None:
        status = plan.status or 200
        body = _encode_body(plan)
        declared = len(body) + TRUNCATION_PAD if plan.action == "truncated_body" else len(body)

        headers: dict[str, str] = {"Content-Type": _content_type(plan)}
        headers.update({name: value for name, value in plan.headers})
        headers["Content-Length"] = str(declared)
        headers["Connection"] = "close"

        head = [f"HTTP/1.1 {status} {_reason(status)}".rstrip()]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        self.wfile.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        self.wfile.write(body)
        self.wfile.flush()


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _content_type(plan: _Plan) -> str:
    if isinstance(plan.body, str) and plan.action != "malformed_json":
        return "text/plain; charset=utf-8"
    return "application/json"


def _encode_body(plan: _Plan) -> bytes:
    if plan.body is not None:
        if isinstance(plan.body, str):
            return plan.body.encode("utf-8")
        return json.dumps(jsonable(plan.body), sort_keys=True).encode("utf-8")
    if plan.action == "malformed_json":
        return MALFORMED_JSON_BODY.encode("utf-8")
    if plan.action == "status":
        return json.dumps({"ok": False, "status": plan.status}, sort_keys=True).encode("utf-8")
    return json.dumps(DEFAULT_SUCCESS_BODY, sort_keys=True).encode("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, controller: "HttpFaultServer") -> None:
        self.controller = controller
        self.stopping = threading.Event()
        super().__init__((LOOPBACK, 0), _FaultHandler)


class HttpFaultServer:
    """A loopback HTTP server whose responses come from a scenario's rules."""

    def __init__(
        self,
        scenario: Scenario,
        clock: FakeClock | None = None,
        redact_extra: Sequence[str] | None = None,
    ) -> None:
        self.scenario = scenario
        self.clock = clock or FakeClock()
        self.redact_extra = tuple(redact_extra or ())
        self.request_count = 0
        self.records: list[HttpRequestRecord] = []
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "HttpFaultServer":
        """Bind 127.0.0.1 on an ephemeral port and serve on a daemon thread."""
        if self._server is not None:
            return self
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": POLL_SECONDS},
            name="faultlab-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut down, close the listening socket and join the serving thread."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        server.stopping.set()
        try:
            server.shutdown()
        finally:
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    def __enter__(self) -> "HttpFaultServer":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not running; call start() first")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    # -- request handling --------------------------------------------------

    def begin(
        self,
        method: str,
        path: str,
        headers: Iterable[tuple[str, str]],
        body: bytes,
    ) -> tuple[_Plan, HttpRequestRecord]:
        """Pick the response for a request and open its record."""
        with self._lock:
            self.request_count += 1
            count = self.request_count
            plan = self._plan(count)
            record = HttpRequestRecord(
                request=count,
                method=method,
                path=path,
                headers=redact_headers(headers, self.redact_extra),
                body_sha256=hashlib.sha256(body).hexdigest(),
                action=plan.action,
                status=plan.status,
                rule=plan.rule,
                clock_ms_before=self.clock.now_ms(),
                clock_ms_after=self.clock.now_ms(),
            )
            self.records.append(record)
            return plan, record

    def finish(self, record: HttpRequestRecord) -> None:
        """Close a record once the response has been written or withheld."""
        with self._lock:
            record.clock_ms_after = self.clock.now_ms()

    def _plan(self, count: int) -> _Plan:
        """Resolve the rules matching this request into one response plan."""
        matched: Inject | None = None
        rule_id: str | None = None
        for rule in self.scenario.rules:
            if rule.when != "before" or not rule.applies(count):
                continue
            if rule.inject.kind == "delay":
                self.clock.advance_ms(rule.inject.delay_ms or 0)
            elif rule.inject.kind == "http" and matched is None:
                matched, rule_id = rule.inject, rule.id
        if matched is None:
            return _Plan(action="success", status=200)

        headers = dict(matched.headers)
        if matched.retry_after is not None:
            headers["Retry-After"] = matched.retry_after
        status = matched.status if matched.action == "status" else 200
        return _Plan(
            action=matched.action or "success",
            status=status,
            headers=tuple(headers.items()),
            body=matched.value,
            rule=rule_id,
        )

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """A canonical-JSON-ready summary: no wall clock, no hostnames, no port."""
        with self._lock:
            records = [record.as_dict() for record in self.records]
        return {
            "bind": LOOPBACK,
            "clock_ms": self.clock.now_ms(),
            "request_count": len(records),
            "requests": records,
            "seed": self.scenario.seed,
        }
