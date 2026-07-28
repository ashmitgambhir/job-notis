"""Exception hierarchy.

The split that matters operationally is :class:`TransientError` (worth
retrying, and never a reason to disable a company) versus
:class:`PermanentError` (the config or endpoint is wrong, and retrying just
burns the cycle budget).
"""

from __future__ import annotations

from typing import Optional


class JobMonitorError(Exception):
    """Base for everything this package raises."""


class ConfigError(JobMonitorError):
    """Configuration is missing, malformed, or internally inconsistent."""


class TransientError(JobMonitorError):
    """Temporary failure: timeout, 5xx, connection reset, rate limit.

    The HTTP layer retries these; the orchestrator counts them but keeps the
    company enabled.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(JobMonitorError):
    """The request will not succeed if repeated: 404, bad params, 401."""


class ConnectorError(JobMonitorError):
    """A connector could not turn a response into postings."""

    def __init__(self, message: str, *, company: Optional[str] = None) -> None:
        super().__init__(message)
        self.company = company


class NotificationError(JobMonitorError):
    """A channel failed to deliver."""

    def __init__(self, message: str, *, channel: Optional[str] = None, permanent: bool = False):
        super().__init__(message)
        self.channel = channel
        self.permanent = permanent
