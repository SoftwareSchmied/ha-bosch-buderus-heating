"""RRC 2.0 contract of the system: status, maintenance, energy history.

Owned by work package W5. Empty tables change nothing; see
``docs/dev/rrc-packages.md`` for what each table does.
"""

from __future__ import annotations

from .rrc_contract_base import RrcContract

CONTRACT = RrcContract(name="system")
