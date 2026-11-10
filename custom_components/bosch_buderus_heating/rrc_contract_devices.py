"""RRC 2.0 contract of the radio devices: details and settings.

Owned by work package W6. Empty tables change nothing; see
``docs/dev/rrc-packages.md`` for what each table does.
"""

from __future__ import annotations

from .rrc_contract_base import RrcContract

CONTRACT = RrcContract(name="devices")
