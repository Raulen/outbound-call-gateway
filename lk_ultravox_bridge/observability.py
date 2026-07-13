from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .config import BridgeConfig

APP_NAME = "outbound-call-gateway"


class LokiShipper(logging.Handler):
    """Ships log records to Grafana Cloud Loki in background batches.

    Design constraints, in order of importance:
    - This process also moves live phone audio: emit() must never block or
      raise.  Records go into a bounded queue and are dropped (and counted)
      when it is full — losing telemetry is always preferable to degrading
      a call.
    - No recursive logging: shipper failures go to stderr, rate-limited to
      one line per minute.
    - stdout logging is untouched; this is an additional sink.  The Render
      log stream remains the debugging fallback if Loki is unreachable.

    Label discipline: only low-cardinality stream labels (app/env + level).
    High-cardinality context (call id, room) stays inside the log line and
    is queried with LogQL filters, never as a label.
    """

    def __init__(
        self,
        url: str,
        user: str,
        token: str,
        labels: Dict[str, str],
        *,
        batch_size: int = 100,
        flush_interval_s: float = 2.0,
        queue_size: int = 10_000,
        transport: Optional[httpx.BaseTransport] = None,
        autostart: bool = True,
    ):
        super().__init__()
        self._push_url = url.rstrip("/") + "/loki/api/v1/push"
        self._auth = (user, token)
        self._labels = dict(labels)
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: "queue.Queue[Tuple[int, str, str]]" = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self._last_error_at = 0.0
        self._client = httpx.Client(timeout=10.0, transport=transport)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        if autostart:
            self._thread = threading.Thread(target=self._run, name="loki-shipper", daemon=True)
            self._thread.start()

    # -- logging.Handler interface -------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = (int(record.created * 1e9), self.format(record), record.levelname.lower())
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        else:
            while self._flush_once():
                pass
        self._client.close()
        super().close()

    # -- shipping loop ---------------------------------------------------

    def _run(self) -> None:
        # wait() doubles as the flush timer; returns True once stop is set.
        while not self._stop.wait(self._flush_interval_s):
            while self._flush_once():
                pass
        while self._flush_once():  # final drain on shutdown
            pass

    def _flush_once(self) -> int:
        """Ship up to one batch synchronously; returns how many were sent."""
        batch: List[Tuple[int, str, str]] = []
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._ship(batch)
        return len(batch)

    def _ship(self, batch: List[Tuple[int, str, str]]) -> None:
        by_level: Dict[str, List[List[str]]] = {}
        for ts_ns, line, level in batch:
            by_level.setdefault(level, []).append([str(ts_ns), line])
        payload = {
            "streams": [
                {"stream": {**self._labels, "level": level}, "values": values}
                for level, values in by_level.items()
            ]
        }
        try:
            resp = self._client.post(self._push_url, json=payload, auth=self._auth)
            if resp.status_code >= 400:
                self._report_error(f"Loki push HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:  # network blip: drop the batch, never raise
            self._report_error(f"Loki push failed: {e!r}")

    def _report_error(self, msg: str) -> None:
        now = time.time()
        if now - self._last_error_at >= 60.0:
            self._last_error_at = now
            sys.stderr.write(f"[observability] {msg} (dropped={self.dropped})\n")


def build_loki_handler(cfg: BridgeConfig) -> Optional[LokiShipper]:
    """Returns a shipping handler, or None when Grafana is not configured.

    Observability is strictly optional: dev machines and tests without
    GRAFANA_* env vars run exactly as before, stdout only.
    """
    if not (cfg.grafana_loki_url and cfg.grafana_loki_user and cfg.grafana_token):
        return None
    return LokiShipper(
        cfg.grafana_loki_url,
        cfg.grafana_loki_user,
        cfg.grafana_token,
        labels={"app": APP_NAME, "env": cfg.environment},
    )
