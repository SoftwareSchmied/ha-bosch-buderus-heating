"""Keys for the private iCom holiday reset marker cache."""

import hashlib

from .const import DOMAIN


def icom_reset_store_key(
    entry_id: str, gateway_id: str, *, legacy: bool = False
) -> str:
    """Use an entry-scoped key and a hashed gateway identifier."""
    if legacy:
        digest = hashlib.sha256(f"{entry_id}:{gateway_id}".encode()).hexdigest()
        return f"{DOMAIN}.icom_reset.{digest}"
    digest = hashlib.sha256(gateway_id.encode()).hexdigest()
    return f"{DOMAIN}.icom_reset.{entry_id}.{digest}"
