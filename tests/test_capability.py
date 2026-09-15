"""Hermetic tests for the fail-closed capability record and schema sync."""

from __future__ import annotations

import json
import os

from papaya_agent_runtime.probes.capability import (
    CAPABILITY_FIELDS,
    Capabilities,
    CapabilityRecord,
    ScenarioResult,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCHEMA = os.path.join(_REPO_ROOT, "schemas", "provider-capability.schema.json")


def test_capabilities_default_to_false() -> None:
    caps = Capabilities()
    for flag in CAPABILITY_FIELDS:
        assert getattr(caps, flag) is False, f"{flag} must default to False (fail-closed)"


def test_capability_fields_match_dataclass() -> None:
    assert set(CAPABILITY_FIELDS) == set(Capabilities().to_dict())


def test_capability_fields_match_schema() -> None:
    with open(_SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    schema_props = schema["properties"]["capabilities"]["properties"]
    assert set(CAPABILITY_FIELDS) == set(schema_props)
    required = schema["properties"]["capabilities"]["required"]
    assert set(CAPABILITY_FIELDS) == set(required)


def test_record_serializes_round_trip() -> None:
    record = CapabilityRecord(
        provider="claude",
        cli_version="2.1.251",
        probed_at="2026-08-28T00:00:00+00:00",
        probe_tool_version="0.0.0",
        model="haiku",
        scenarios=[ScenarioResult("clean_complete", "proved", "ok", "sid-1", 1.2)],
    )
    record.capabilities.resume_after_clean_exit = True
    data = record.to_dict()
    assert data["provider"] == "claude"
    assert data["capabilities"]["resume_after_clean_exit"] is True
    assert data["capabilities"]["resume_after_sigint_tool"] is False
    assert data["scenarios"][0]["status"] == "proved"
    # Serializable as JSON.
    json.dumps(data)
