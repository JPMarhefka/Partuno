# Partuno capabilities

Partuno provides a provider-neutral MCP surface for component research and
sourcing. Results are designed to keep source evidence, normalized values,
provider status, and uncertainty visible to the assistant and operator.

## Research and comparison

- Search catalog and parametric data from DigiKey and Mouser.
- Retrieve exact product details, manufacturer part numbers, packaging,
  stock, lifecycle, compliance, and technical attributes.
- Compare exact manufacturer/MPN identity across providers.
- Calculate requested-quantity offer totals while preserving minimum order
  quantities, order multiples, price breaks, stock, and missing values.
- Return partial provider results when one provider fails or is rate limited;
  a provider failure is not silently treated as an empty catalog.

## Recommendations and BOM analysis

Recommendations evaluate hard requirements using three explicit states:

- `meets`: the available evidence satisfies the requirement.
- `does_not_meet`: the available evidence conflicts with the requirement.
- `unknown`: the required attribute is missing, ambiguous, or could not be
  verified from the provider response.

The normalizer converts common distributor-specific names and units into a
shared representation without inventing values. BOM workflows can combine
stock, lifecycle, compliance, pricing, tariff, lead-time, and substitute
signals. Pareto shortlists and evidence summaries make tradeoffs visible
instead of hiding them in a single opaque score.

## Account and project workflows

Depending on provider access and the operator's credentials, the server can
support bounded workflows including:

- DigiKey MyLists, quote previews, order status, product-change notices,
  barcodes, and packing-list or receiving workflows.
- Mouser read-only order history, cart preview, and explicitly confirmed cart
  changes.
- Receiving reconciliation and lifecycle or availability checks.

Provider availability and account permissions vary. A tool response should be
read as a current provider result, not a guarantee that an item can be ordered.

## Safety boundary

| Workflow | Default behavior | Confirmation boundary |
| --- | --- | --- |
| Catalog search, product detail, comparison, recommendation | Read-only | No confirmation needed |
| MyList or quote changes | Preview or explicit mutation | Requires the tool's confirmation input |
| Mouser cart changes | Preview first | Requires a one-time execution token and confirmation |
| Distributor order submission | Not implemented | Partuno cannot submit an order |

Read-only tools are annotated as safe reads for MCP clients. Write-capable
tools expose their mutation boundary in their descriptors and reject missing
confirmation. Credentials are operator-owned, are not returned by tools, and
should never be placed in prompts, screenshots, or source control.

## Connection surfaces

Native MCP is the primary interface and is available through local stdio,
loopback HTTP, or an operator-owned remote HTTPS deployment. The existing REST
and Custom GPT Action/OpenAPI endpoints remain available for backward
compatibility, including `/action-openapi.json`.

See [MCP setup](../MCP_SETUP.md) for connection instructions and the
[deployment guides](deployment/local.md) for the supported hosting modes.
