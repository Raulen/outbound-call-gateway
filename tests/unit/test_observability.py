"""LokiShipper contract: correct push payload, never blocks or raises in the
logging path, and stays disabled when Grafana is not configured."""
from __future__ import annotations

import json
import logging

import httpx

from lk_ultravox_bridge.observability import LokiShipper, build_loki_handler

from tests.conftest import make_config


def make_record(msg: str, level: int = logging.INFO, name: str = "sqs-worker") -> logging.LogRecord:
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=1,
                             msg=msg, args=(), exc_info=None)


class CapturingTransport(httpx.MockTransport):
    def __init__(self, status_code: int = 204):
        self.requests: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status_code)

        super().__init__(handler)


def make_shipper(transport, **kwargs) -> LokiShipper:
    # autostart=False: tests drive flushing synchronously, no thread timing.
    return LokiShipper("https://loki.test", "12345", "glc_token",
                       labels={"app": "outbound-call-gateway", "env": "test"},
                       transport=transport, autostart=False, **kwargs)


class TestBuildFactory:
    def test_disabled_without_grafana_config(self):
        assert build_loki_handler(make_config()) is None  # conftest leaves GRAFANA_* empty

    def test_enabled_with_full_config_and_labels_from_env(self):
        cfg = make_config(grafana_loki_url="https://loki.test", grafana_loki_user="1",
                          grafana_token="t", environment="prod")
        handler = build_loki_handler(cfg)
        try:
            assert isinstance(handler, LokiShipper)
            assert handler._labels == {"app": "outbound-call-gateway", "env": "prod"}
        finally:
            handler.close()

    def test_partial_config_stays_disabled(self):
        cfg = make_config(grafana_loki_url="https://loki.test")  # no user/token
        assert build_loki_handler(cfg) is None


class TestShipping:
    def test_payload_shape_streams_grouped_by_level(self):
        transport = CapturingTransport()
        shipper = make_shipper(transport)
        shipper.emit(make_record("[SQS] SIP dial answered id=1"))
        shipper.emit(make_record("[UV->LK] buffer overflow", level=logging.WARNING))
        shipper.close()

        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.url.path == "/loki/api/v1/push"
        assert "Authorization" in req.headers  # basic auth present

        payload = json.loads(req.content)
        streams = {s["stream"]["level"]: s for s in payload["streams"]}
        assert set(streams) == {"info", "warning"}
        for s in streams.values():
            assert s["stream"]["app"] == "outbound-call-gateway"
            assert s["stream"]["env"] == "test"
        # line keeps the logger name prefix; timestamp is a ns string
        ts, line = streams["info"]["values"][0]
        assert line == "sqs-worker: [SQS] SIP dial answered id=1"
        assert ts.isdigit() and len(ts) >= 19

    def test_queue_full_drops_instead_of_blocking(self):
        transport = CapturingTransport()
        shipper = make_shipper(transport, queue_size=2)
        for i in range(5):
            shipper.emit(make_record(f"m{i}"))  # must not raise nor block
        assert shipper.dropped == 3
        shipper.close()
        # the 2 queued lines still shipped on close
        payload = json.loads(transport.requests[0].content)
        assert len(payload["streams"][0]["values"]) == 2

    def test_push_failure_never_raises_into_logging(self):
        shipper = make_shipper(CapturingTransport(status_code=500))
        shipper.emit(make_record("boom"))
        shipper.close()  # flush hits HTTP 500 -> stderr note, no exception

    def test_records_from_the_shipping_thread_are_ignored(self):
        # httpx logs each push at INFO; with the handler on the root logger,
        # ingesting the shipping thread's own logs would be an infinite
        # feedback loop (one push per flush, forever) — a real prod incident.
        import threading

        transport = CapturingTransport()
        shipper = make_shipper(transport)
        shipper._thread = threading.current_thread()  # pretend we ARE the shipping thread
        shipper.emit(make_record("HTTP Request: POST .../loki/api/v1/push", name="httpx"))
        shipper._thread = None
        shipper.close()
        assert transport.requests == []  # nothing queued, nothing shipped
