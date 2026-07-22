"""Grafana contract: the dashboard counts and parses literal log substrings.

That coupling (LogQL `|=` filters and regexps over our log lines) is invisible
in the source — renaming a log message silently zeroes a panel in production.
This test pins every substring the dashboard depends on to the module that
emits it, so a rename breaks here first.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "observability" / "grafana-dashboard.json"
PKG = ROOT / "lk_ultravox_bridge"

# (substring the dashboard filters/parses, module whose log lines carry it)
WATCHED = [
    ("[HB] alive", "sqs_worker.py"),                # worker-down alert + in-flight gauge
    ("inFlight=", "sqs_worker.py"),
    ("TRIGGER_CALL received", "sqs_worker.py"),     # funnel: received
    ("dialing SIP", "sqs_worker.py"),               # funnel: dialing (CALL_ATTEMPT_STARTED)
    ("SIP dial answered", "sqs_worker.py"),         # funnel: answered (CALL_STARTED)
    ("audio bridge finished", "sqs_worker.py"),     # funnel: completed (CALL_ENDED)
    ("durationS=", "sqs_worker.py"),                # call duration unwrap
    ("endReason=", "sqs_worker.py"),                # completion-cause breakdown
    ("call not answered", "sqs_worker.py"),         # unreachable-callee categories
    ("reason=", "sqs_worker.py"),
    ("processing failed", "sqs_worker.py"),         # failures: processing
    ("errorType=", "sqs_worker.py"),                # failure-by-type breakdown
    ("receiveCount=", "sqs_worker.py"),             # DLQ-bound stat (redrive at 5)
    ("receive failed", "sqs_worker.py"),            # failures: infra
    ("crashed unexpectedly", "sqs_worker.py"),
    ("buffer overflow", "audio_bridge.py"),         # audio quality
    ("watchdog", "audio_bridge.py"),
    ("[LiveKit][SIP] ok", "livekit_client.py"),     # time-to-answer
    ("elapsedMs=", "livekit_client.py"),
]


class TestGrafanaContract:
    def test_every_watched_substring_exists_in_dashboard_and_source(self):
        dashboard = DASHBOARD.read_text(encoding="utf-8")
        for needle, module in WATCHED:
            source = (PKG / module).read_text(encoding="utf-8")
            assert needle in dashboard, f"dashboard no longer watches {needle!r}"
            assert needle in source, (
                f"{module} no longer logs {needle!r} — a Grafana panel that "
                f"parses this substring would silently flatline"
            )
