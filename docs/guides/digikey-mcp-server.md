# DigiKey MCP Server for Electronic Component Research | Partuno

Partuno is a provider-neutral, local-first **DigiKey MCP server** for
electronic component search, product research, BOM analysis, sourcing
comparison, lifecycle checks, and bounded distributor workflows. It connects an
MCP client to the operator's own DigiKey credentials; it is not an official
DigiKey service and does not provide shared credentials.

## What the DigiKey MCP server does

Use Partuno to:

- Search by keyword, manufacturer part number, or DigiKey product number.
- Retrieve product details, stock, lifecycle, lead time, classifications,
  pricing, datasheets, images, substitutes, and product-change notices.
- Analyze BOMs for current merchandise cost, shortages, end-of-life risk,
  compliance, lead time, tariffs, substitutes, and alternate packaging.
- Preview and explicitly confirm supported MyList and quote changes.
- Decode product-bag or packing-list barcodes and reconcile received quantities.

<p align="center">
  <img src="../assets/demos/product-research.gif" alt="Partuno DigiKey MCP server researching an electronic component" width="900">
</p>

## Setup

The default deployment is local MCP over stdio. Partuno is published on PyPI,
so the shortest setup is:

```bash
uvx --from partuno partuno-mcp
```

Credentials remain in the operator's environment and no public network port is
required. See the [local deployment guide](../deployment/local.md) for the
supported credential variables and the [native MCP setup guide](../../MCP_SETUP.md)
for operator-owned remote deployments. Use the source-checkout path only when
contributing or testing unreleased changes.

## Supported MCP workflows

The most relevant read-only tools include `search_products`,
`get_product_details`, `get_product_pricing`, `research_product`,
`analyze_bom`, `audit_lifecycle`, `get_substitutions`, and
`get_product_change_notifications`. Account workflows include MyLists, quotes,
order status, barcodes, and packing lists when the operator's DigiKey access
allows them.

For the complete surface and safety boundary, see the
[capability reference](../capabilities.md).

## Limitations and safety

- You must supply and authorize your own DigiKey credentials.
- Product, stock, pricing, and lead-time values are live provider data and can
  change.
- Missing or ambiguous engineering attributes remain `unknown`.
- Manufacturer lead time is not a shipping estimate; shipping cost and delivery
  ETA are not provided by the comparison workflow.
- Partuno does not submit distributor orders or silently execute account changes.

## Links

- [Partuno source repository](https://github.com/JPMarhefka/Partuno)
- [Partuno on Glama](https://glama.ai/mcp/servers/JPMarhefka/partuno)
- [Mouser MCP server guide](mouser-mcp-server.md)
- [Electronics BOM MCP server guide](electronics-bom-mcp.md)
