"""YAML loading with environment interpolation.

Secrets never live in the YAML files. Instead the YAML references them as
``${TWILIO_AUTH_TOKEN}`` and this module resolves them from the environment (or
a ``.env`` file) before validation, so a committed config is always safe.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..errors import ConfigError
from .schema import (
    AppConfig,
    CompaniesConfig,
    FiltersConfig,
    NotificationsConfig,
    RolesConfig,
    Settings,
)

# ${VAR}, ${VAR:-fallback}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

CONFIG_FILES = {
    "settings": "settings.yaml",
    "companies": "companies.yaml",
    "roles": "roles.yaml",
    "filters": "filters.yaml",
    "notifications": "notifications.yaml",
}


def load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a .env file without clobbering real env vars.

    Real environment variables win, so a container's injected secrets always
    take precedence over a stale local file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _interpolate(value: Any) -> Any:
    """Recursively replace ``${VAR}`` references."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            resolved = os.environ.get(name)
            # POSIX `${VAR:-default}` semantics: the default applies when the
            # variable is unset *or empty*. Handling only "unset" broke every
            # CI deployment — GitHub Actions materialises a missing secret as
            # an empty string, so `${LLM_ENABLED:-false}` resolved to "" and
            # then to None, and boolean fields failed validation.
            if resolved is None or resolved == "":
                if default is None:
                    # An unset secret with no default is normal (that channel
                    # is simply off); collapse to empty rather than exploding
                    # at load time. The channel's own validation reports it.
                    return ""
                return default
            return resolved

        out = _ENV_PATTERN.sub(repl, value)
        return out if out != "" else None if _ENV_PATTERN.fullmatch(value) else out
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        # An unset secret inside a list (`to_numbers: [${ALERT_PHONE_NUMBER}]`)
        # means "no recipient configured", not "a recipient named null" — so
        # drop it rather than failing validation for a channel that is off.
        return [item for item in (_interpolate(v) for v in value) if item is not None]
    return value


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return _interpolate(data)


def _apply_company_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``defaults:`` into every company entry.

    Keeps companies.yaml readable at 500 entries: shared settings are declared
    once instead of repeated per company.
    """
    defaults = raw.get("defaults") or {}
    companies = raw.get("companies") or []
    merged = []
    for entry in companies:
        if not isinstance(entry, dict):
            raise ConfigError(f"company entries must be mappings, got {type(entry).__name__}")
        combined = dict(defaults)
        combined.update(entry)
        # params merge one level deep so a company can override a single key.
        if "params" in defaults and "params" in entry:
            params = dict(defaults["params"])
            params.update(entry["params"] or {})
            combined["params"] = params
        merged.append(combined)
    raw["companies"] = merged
    return raw


def load_config(config_dir: Optional[Path] = None, env_file: Optional[Path] = None) -> AppConfig:
    """Load and validate the full configuration set."""
    config_dir = Path(config_dir or os.environ.get("JOBMON_CONFIG_DIR", "config"))
    load_dotenv(Path(env_file or os.environ.get("JOBMON_ENV_FILE", ".env")))

    if not config_dir.is_dir():
        raise ConfigError(f"config directory not found: {config_dir}")

    raw = {key: _read_yaml(config_dir / name) for key, name in CONFIG_FILES.items()}
    raw["companies"] = _apply_company_defaults(raw["companies"])

    try:
        return AppConfig(
            settings=Settings(**raw["settings"]),
            companies=CompaniesConfig(**raw["companies"]),
            roles=RolesConfig(**raw["roles"]),
            filters=FiltersConfig(**raw["filters"]),
            notifications=NotificationsConfig(**raw["notifications"]),
        )
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"configuration is invalid:\n{exc}") from exc


def save_companies(config_dir: Path, companies_doc: Dict[str, Any]) -> None:
    """Write companies.yaml back out, used by ``jobmon discover``."""
    path = Path(config_dir) / CONFIG_FILES["companies"]
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(companies_doc, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    tmp.replace(path)


def read_companies_doc(config_dir: Path) -> Dict[str, Any]:
    """Read companies.yaml *without* interpolation or default merging.

    ``discover`` rewrites this file, and expanding defaults or secrets first
    would bake them permanently into every entry.
    """
    path = Path(config_dir) / CONFIG_FILES["companies"]
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
