"""Connector registry.

Maps the ``connector:`` value in companies.yaml to a class. Adding a platform
means writing one module and adding one line here — no changes anywhere else in
the system.

The ordering in :data:`PREFERENCE` encodes the project's central rule: real
APIs first, structured data next, HTML after that, and a browser only when
nothing else works. Discovery walks this list in order.
"""

from __future__ import annotations

from typing import Dict, List, Type

from ..errors import ConfigError
from .amazon import AmazonConnector
from .ashby import AshbyConnector
from .avature import AvatureConnector
from .base import Connector
from .browser import BrowserConnector
from .eightfold import EightfoldConnector
from .greenhouse import GreenhouseConnector
from .html_generic import HtmlConnector
from .icims import IcimsConnector
from .jobvite import JobviteConnector
from .jsonld import JsonLdConnector
from .lever import LeverConnector
from .oracle import OracleConnector
from .phenom import PhenomConnector
from .smartrecruiters import SmartRecruitersConnector
from .successfactors import SuccessFactorsConnector
from .taleo import TaleoConnector
from .usajobs import UsaJobsConnector
from .workday import WorkdayConnector

_CONNECTORS: List[Type[Connector]] = [
    GreenhouseConnector,
    LeverConnector,
    AshbyConnector,
    SmartRecruitersConnector,
    WorkdayConnector,
    SuccessFactorsConnector,
    OracleConnector,
    IcimsConnector,
    AshbyConnector,
    JobviteConnector,
    EightfoldConnector,
    PhenomConnector,
    AvatureConnector,
    TaleoConnector,
    AmazonConnector,
    UsaJobsConnector,
    JsonLdConnector,
    HtmlConnector,
    BrowserConnector,
]

REGISTRY: Dict[str, Type[Connector]] = {c.name: c for c in _CONNECTORS}

#: Discovery probe order. Cheapest and most reliable sources first; the
#: browser is deliberately last and is never selected automatically.
PREFERENCE: List[str] = [
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workday",
    "successfactors",
    "oracle",
    "icims",
    "jobvite",
    "eightfold",
    "phenom",
    "avature",
    "taleo",
    "jsonld",
    "html",
]


def get_connector_class(name: str) -> Type[Connector]:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ConfigError(
            f"unknown connector '{name}'. Available: {', '.join(sorted(REGISTRY))}"
        ) from None


def available() -> List[str]:
    return sorted(REGISTRY)
