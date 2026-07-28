"""Health endpoint and self-monitoring.

Two consumers: container/platform liveness probes (Railway, Render, Fly, Cloud
Run all want an HTTP 200), and you, when you want to know whether the thing is
still running without SSHing in.

The heartbeat is deliberately based on *scan completion* rather than process
liveness — a process that is up but whose scheduler has wedged is exactly the
failure this is meant to catch.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable, Dict, Optional

import structlog

from .models import utcnow

log = structlog.get_logger(__name__)


class HealthState:
    """Mutable snapshot the HTTP endpoint reads."""

    def __init__(self, heartbeat_timeout_seconds: int) -> None:
        self.heartbeat_timeout = heartbeat_timeout_seconds
        self.started_at = utcnow()
        self.last_cycle_at: Optional[datetime] = None
        self.last_cycle_duration: float = 0.0
        self.last_cycle_failures: int = 0
        self.companies_scanned: int = 0
        self.total_cycles: int = 0
        self.total_matches: int = 0
        self.total_notifications: int = 0
        self.last_error: Optional[str] = None

    def record_cycle(self, cycle: Any) -> None:
        self.last_cycle_at = cycle.finished_at
        self.last_cycle_duration = cycle.duration_seconds
        self.last_cycle_failures = len(cycle.failures)
        self.companies_scanned = len(cycle.companies)
        self.total_cycles += 1
        self.total_matches += cycle.jobs_matched
        self.total_notifications += cycle.notifications_sent

    @property
    def healthy(self) -> bool:
        if self.last_cycle_at is None:
            # Give the first cycle a grace period equal to the timeout.
            return (utcnow() - self.started_at).total_seconds() < self.heartbeat_timeout
        age = (utcnow() - self.last_cycle_at).total_seconds()
        return age < self.heartbeat_timeout

    def snapshot(self) -> Dict[str, Any]:
        age = (
            (utcnow() - self.last_cycle_at).total_seconds()
            if self.last_cycle_at
            else None
        )
        return {
            "status": "healthy" if self.healthy else "unhealthy",
            "uptime_seconds": round((utcnow() - self.started_at).total_seconds(), 1),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "seconds_since_last_cycle": round(age, 1) if age is not None else None,
            "last_cycle_duration_seconds": round(self.last_cycle_duration, 1),
            "last_cycle_failures": self.last_cycle_failures,
            "companies_scanned": self.companies_scanned,
            "total_cycles": self.total_cycles,
            "total_matches": self.total_matches,
            "total_notifications": self.total_notifications,
            "last_error": self.last_error,
        }


class HealthServer:
    """Minimal asyncio HTTP server — no web framework needed for two routes."""

    def __init__(self, state: HealthState, port: int,
                 extra: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.state = state
        self.port = port
        self.extra = extra
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, host="0.0.0.0", port=self.port
            )
            log.info("health_server_listening", port=self.port)
        except OSError as exc:
            # A busy port must not stop the scanner from running.
            log.warning("health_server_failed", port=self.port, error=str(exc))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            path = "/"
            parts = request.decode("latin-1").split()
            if len(parts) >= 2:
                path = parts[1]

            payload = self.state.snapshot()
            if path.startswith("/metrics") and self.extra is not None:
                payload.update(self.extra())

            healthy = self.state.healthy
            status = "200 OK" if healthy else "503 Service Unavailable"
            body = json.dumps(payload, indent=2).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
