"""Opt-in local canaries for AGY session settings.

These tests intentionally skip unless both an explicit environment opt-in and
an authorization reference are supplied. Passing this module without the
opt-in is not evidence that session settings work on the current CLI.
"""

from __future__ import annotations

import os

import pytest

from codex_agy_bridge.agy_policy import probe_agy_policy


@pytest.mark.skipif(
    os.environ.get("AGY_POLICY_PROBE_OPT_IN") != "1"
    or not os.environ.get("AGY_POLICY_AUTHORIZATION_REF"),
    reason="explicit local AGY policy canary was not authorized",
)
def test_local_policy_canary_records_scoped_evidence() -> None:
    result = probe_agy_policy(
        os.environ["AGY_POLICY_AUTHORIZATION_REF"],
        timeout=60,
    )

    assert result["state"] == "verified"
    assert result["checks"]
    assert all(result["checks"].values())
