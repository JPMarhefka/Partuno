from __future__ import annotations

import hashlib
import json
from typing import Any


LOCAL_PRINCIPAL = "local"


def digikey_subject(associated_accounts: Any) -> str:
    """Derive a stable opaque principal from DigiKey's verified account set."""
    canonical = json.dumps(
        associated_accounts,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"digikey:{digest}"
