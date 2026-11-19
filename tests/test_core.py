"""Tests standard tap features using the built-in SDK tests library."""

from __future__ import annotations

from singer_sdk.testing import SuiteConfig, get_tap_test_class

from tap_microsoft_dynamics_bc.tap import TapMicrosoftDynamicsBC
from tests.conftest import SAMPLE_CONFIG

# Run standard built-in tap tests from the SDK against the mocked environment.
# `trial_balances` answers 403 in the mock to exercise the skip-unavailable path.
TestTapMicrosoftDynamicsBC = get_tap_test_class(
    tap_class=TapMicrosoftDynamicsBC,
    config=SAMPLE_CONFIG,
    suite_config=SuiteConfig(ignore_no_records_for_streams=["trial_balances"]),
    # The SDK defaults to True, which loads a developer's real `.env` over the mock config.
    parse_env_config=False,
)
