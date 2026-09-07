"""Configuration loading and guardrail enforcement.

Two ideas live in this file:

1. The screening criteria are data, not code. They sit in config/criteria.json
   so a non-developer can change a threshold and re-run.
2. The guardrails are enforced here, at startup. If someone edits
   config/guardrails.json to grant write access or web access, the engine
   refuses to run rather than quietly doing the dangerous thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths. Everything is relative to the repository root so the prototype runs
# the same way from any working directory.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

CRITERIA_PATH = CONFIG_DIR / "criteria.json"
GUARDRAILS_PATH = CONFIG_DIR / "guardrails.json"


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or unsafe."""


class GuardrailViolation(RuntimeError):
    """Raised when the configured guardrails have been weakened."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}\n"
            f"Expected it at {path.relative_to(REPO_ROOT)} inside the repository."
        )
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}\n"
            f"A missing or extra comma is the usual cause."
        ) from exc


def _require(mapping: dict[str, Any], key: str, source: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required setting '{key}' in {source}.")
    return mapping[key]


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guardrails:
    """The safety envelope the engine runs inside."""

    read_only: bool
    web_access_enabled: bool
    allowlisted_tools: tuple[str, ...]
    promotions_require_human_approval: bool
    borderline_cases_require_human_review: bool
    agent_may_send_external_messages: bool
    audit_log_path: Path
    review_queue_dir: Path
    state_dir: Path
    active_data_source: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def assert_tool_allowed(self, tool_name: str) -> None:
        """Gate every data access. Nothing outside the allowlist runs."""
        if tool_name not in self.allowlisted_tools:
            raise GuardrailViolation(
                f"Tool '{tool_name}' is not on the allowlist. "
                f"Permitted tools: {', '.join(self.allowlisted_tools)}."
            )

    def assert_write_allowed(self, target: Path) -> None:
        """The engine may only write into the review queue and state dirs."""
        resolved = Path(target).resolve()
        permitted = (self.review_queue_dir.resolve(), self.state_dir.resolve())
        for allowed_dir in permitted:
            if resolved == allowed_dir or allowed_dir in resolved.parents:
                return
        if resolved == self.audit_log_path.resolve():
            return
        raise GuardrailViolation(
            f"Refusing to write to {resolved}. The engine may only write to "
            f"{', '.join(str(p.relative_to(REPO_ROOT)) for p in permitted)} "
            f"and the audit log."
        )


def load_guardrails(path: Path = GUARDRAILS_PATH) -> Guardrails:
    """Load guardrails and refuse to continue if they have been weakened."""
    raw = _load_json(path)
    source = path.name

    read_only = bool(_require(raw, "read_only", source))
    web_access = bool(_require(raw, "web_access_enabled", source))
    tools = tuple(_require(raw, "allowlisted_tools", source))
    hitl = _require(raw, "human_in_the_loop", source)
    audit = _require(raw, "audit", source)
    outputs = _require(raw, "outputs", source)
    data_sources = _require(raw, "data_sources", source)

    # --- the non-negotiables -------------------------------------------------
    if not read_only:
        raise GuardrailViolation(
            "guardrails.json sets read_only=false. This prototype is read-only "
            "by design; it has no write connectors to any external system."
        )
    if web_access:
        raise GuardrailViolation(
            "guardrails.json sets web_access_enabled=true. The agent's tool set "
            "must contain only the allowlisted data tools, with no open web access."
        )
    if hitl.get("agent_may_send_external_messages"):
        raise GuardrailViolation(
            "guardrails.json permits external messages. The agent never sends "
            "anything anywhere; all output lands in the local review queue."
        )
    if not hitl.get("promotions_require_human_approval", False):
        raise GuardrailViolation(
            "guardrails.json disables human approval for promotions. "
            "Watchlist-to-pipeline promotions must be proposed, never executed."
        )
    if not tools:
        raise GuardrailViolation(
            "guardrails.json has an empty tool allowlist; the engine would have "
            "no way to read data."
        )
    if not audit.get("log_every_tool_call", False):
        raise GuardrailViolation(
            "guardrails.json disables tool-call logging. Every data access must "
            "be written to the audit log."
        )

    active = str(_require(data_sources, "active", source))
    available = _require(data_sources, "available", source)
    if active not in available:
        raise ConfigError(
            f"Active data source '{active}' is not listed under "
            f"data_sources.available in {source}."
        )
    if not available[active].get("enabled", False):
        raise ConfigError(
            f"Active data source '{active}' is marked enabled=false in {source}."
        )

    return Guardrails(
        read_only=read_only,
        web_access_enabled=web_access,
        allowlisted_tools=tools,
        promotions_require_human_approval=True,
        borderline_cases_require_human_review=bool(
            hitl.get("borderline_cases_require_human_review", True)
        ),
        agent_may_send_external_messages=False,
        audit_log_path=REPO_ROOT / audit["log_path"],
        review_queue_dir=REPO_ROOT / outputs["review_queue_dir"],
        state_dir=REPO_ROOT / outputs["state_dir"],
        active_data_source=active,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Screening criteria
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Criteria:
    """The investment screen, loaded from config/criteria.json."""

    version: str
    data_label: str
    listing: dict[str, Any]
    ownership: dict[str, Any]
    financial: dict[str, Any]
    geography: dict[str, Any]
    classification: dict[str, Any]
    triggers: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- geography helpers ---------------------------------------------------

    def region_for_country(self, country_code: str) -> str | None:
        """Map 'DE' -> 'DACH'. Returns None if the country is out of scope."""
        code = (country_code or "").upper()
        for region, spec in self.geography["regions"].items():
            if code in spec["countries"]:
                return region
        return None

    def tier_for_country(self, country_code: str) -> str | None:
        """Map 'DE' -> 'primary', 'GB' -> 'secondary', 'US' -> None."""
        region = self.region_for_country(country_code)
        if region is None:
            return None
        return self.geography["regions"][region]["tier"]

    # -- threshold helpers ---------------------------------------------------

    @property
    def min_ebitda(self) -> float:
        return float(self.financial["min_ebitda_eur_m"])

    @property
    def ebitda_borderline_band(self) -> tuple[float, float]:
        low, high = self.financial["borderline_ebitda_band_eur_m"]
        return float(low), float(high)

    @property
    def ebitda_hard_floor(self) -> float:
        return float(self.financial["reject_below_eur_m"])

    @property
    def max_pe_stake(self) -> float:
        return float(self.ownership["max_pe_stake_pct"])

    @property
    def min_family_stake(self) -> float:
        return float(self.ownership["min_family_founder_stake_pct"])

    @property
    def trigger_categories(self) -> tuple[str, ...]:
        return tuple(self.triggers["categories"])

    @property
    def min_trigger_confidence(self) -> float:
        return float(self.triggers["min_confidence_to_confirm"])


def load_criteria(path: Path = CRITERIA_PATH) -> Criteria:
    raw = _load_json(path)
    source = path.name

    for key in (
        "listing",
        "ownership",
        "financial",
        "geography",
        "classification",
        "triggers",
    ):
        _require(raw, key, source)

    criteria = Criteria(
        version=str(raw.get("version", "unknown")),
        data_label=str(raw.get("data_label", "MOCK DATA")),
        listing=raw["listing"],
        ownership=raw["ownership"],
        financial=raw["financial"],
        geography=raw["geography"],
        classification=raw["classification"],
        triggers=raw["triggers"],
        raw=raw,
    )

    # Sanity checks that catch the mistakes a human editing JSON actually makes.
    if criteria.ebitda_hard_floor > criteria.min_ebitda:
        raise ConfigError(
            f"financial.reject_below_eur_m ({criteria.ebitda_hard_floor}) is above "
            f"financial.min_ebitda_eur_m ({criteria.min_ebitda}). The hard floor "
            f"must sit at or below the headline minimum."
        )
    low, high = criteria.ebitda_borderline_band
    if low > high:
        raise ConfigError(
            f"financial.borderline_ebitda_band_eur_m is reversed: [{low}, {high}]."
        )
    if not criteria.classification.get("require_confirmed_trigger_for_active_pipeline", False):
        raise ConfigError(
            "criteria.json sets require_confirmed_trigger_for_active_pipeline=false. "
            "Structural fit without a behavioural trigger must go to the watchlist."
        )
    if criteria.classification.get("structural_fit_without_trigger") != "watchlist":
        raise ConfigError(
            "criteria.json must route structural fit without a trigger to "
            "'watchlist'."
        )
    return criteria


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Everything the engine needs to start up."""

    criteria: Criteria
    guardrails: Guardrails

    def ensure_output_dirs(self) -> None:
        for directory in (self.guardrails.review_queue_dir, self.guardrails.state_dir):
            directory.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """Load criteria + guardrails, or fail loudly with a readable message."""
    return Settings(criteria=load_criteria(), guardrails=load_guardrails())
