from __future__ import annotations

import json
from pathlib import Path


def test_pricing_stress_manifest_uses_registered_tool_contracts() -> None:
    manifest = json.loads(
        (Path(__file__).parents[1] / "MCP_STRESS_TEST_MANIFEST.json").read_text()
    )
    cases = {case["id"]: case for case in manifest["cases"]}

    current = cases["digikey-current-pricing"]
    assert current["tool"] == "get_product_pricing"
    assert "requested_quantity" not in current["arguments"]
    assert "requested_quantity" in current["forbidden_arguments"]
    assert current["arguments"]["exclude_tariff"] is False
    assert {
        "path": "SettingsUsed.ExcludeTariff",
        "equals": False,
    } in current["assertions"]

    quantity = cases["digikey-quantity-pricing-15"]
    assert quantity["tool"] == "get_pricing_by_quantity"
    assert quantity["arguments"]["requested_quantity"] == 15
