# Validation Report

## Result

All offline validation checks passed for DigiKey compatibility, Mouser API
contracts, cross-distributor normalization, and Cart confirmation safety.

```text
155 passed, 4 skipped
```

## OpenAPI schema

```json
{
  "version": "4.0.1",
  "full_paths": 45,
  "full_operations": 52,
  "compact_paths": 34,
  "compact_operations": 38,
  "authorization_parameters_exposed": 0
}
```

## Verified behavior

- Unique operation IDs in both schemas
- No raw `Authorization` parameter exposed to the GPT action schema
- OAuth security metadata included
- Account-changing operations marked consequential
- Full DigiKey search-filter request construction
- Pricing optimizer handles Exact, MinimumOrderQuantity, BetterValue, and quantity-increase restrictions
- MyList diff detects additions, updates, removals, unchanged parts, and duplicate consolidation
- Quote creation uses the Product Quote V4 request shape
- Quote product uploads split into DigiKey's 300-row request batches
- Reference API associated-account path
- Packing-list lookup paths and PDF option
- All four DigiKey barcode families
- Product variation and lifecycle summary parsing
- Mouser Search V1/V2 request shapes and embedded-error handling
- Mouser API-key redaction and minute/day request budgets
- Strict manufacturer-plus-MPN matching and USD price eligibility
- MOQ/order-multiple rounding and applicable price-break selection
- Evidence evaluation, SI-unit conversion, and deterministic Pareto selection
- Read-only Mouser Order History request mappings
- Every documented Cart endpoint with zero automatic write retries
- Cart replacement removal previews and stale/expired/replayed token rejection
- DigiKey bearer validation before protected Mouser REST operations

## Scope of validation

These tests are offline and use mocked distributor responses. Live integrations
are opt-in. Mouser Cart writes require the separate
`MOUSER_CART_WRITE_TESTS=true` approval flag and never submit an order.
