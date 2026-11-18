"""Run tap-microsoft-dynamics-bc as a module."""

from __future__ import annotations

from tap_microsoft_dynamics_bc.tap import TapMicrosoftDynamicsBC

TapMicrosoftDynamicsBC.cli()
