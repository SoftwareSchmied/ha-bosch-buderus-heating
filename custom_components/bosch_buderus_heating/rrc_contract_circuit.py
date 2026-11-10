"""RRC 2.0 contract of the heating circuit: curve, summer/winter, night.

Owned by work package W4. Empty tables change nothing; see
``docs/dev/rrc-packages.md`` for what each table does.
"""

from __future__ import annotations

from .rrc_contract_base import RrcContract

CONTRACT = RrcContract(name="circuit")
