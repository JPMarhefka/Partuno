# Mouser MCP Server for Component Sourcing | Partuno

Partuno is a provider-neutral, local-first **Mouser MCP server** for catalog
search, exact component-offer comparison, BOM sourcing, read-only order
history, and carefully bounded Cart workflows. It uses the operator's own
Mouser API credentials and is not affiliated with or endorsed by Mouser.

## What the Mouser MCP server does

Use Partuno to:

- Search Mouser by keyword or exact part number.
- Inspect availability, lifecycle, compliance, and price-break data.
- Compare the exact manufacturer and MPN across Mouser and DigiKey at a
  requested quantity.
- Read order history and individual order details where the account API allows
  it.
- Read a Cart and preview an exact add, update, remove, replacement, or
  order-copy change before execution.

<p align="center">
  <img src="../assets/demos/exact-offer-comparison.gif" alt="Partuno comparing exact component offers across Mouser and DigiKey" width="900">
</p>

## Setup

For a local deployment, launch the published package and configure the
operator-owned Mouser keys in the environment alongside the DigiKey settings:

```bash
uvx --from partuno partuno-mcp
```

Mouser Search and Account API keys remain separate. They are read from the
deployment configuration and are never supplied as MCP tool parameters.
See the [local deployment guide](../deployment/local.md) for the supported
variables and the [native MCP setup guide](../../MCP_SETUP.md) for
operator-owned remote deployments.

## Supported MCP workflows

The key tools are `search_mouser_products`,
`compare_component_offers`, `search_mouser_order_history`,
`get_mouser_order`, `get_mouser_cart`, `preview_mouser_cart_change`, and
`execute_mouser_cart_change`. The comparison workflow requires strict
manufacturer-plus-MPN identity before it compares requested-quantity offers.

## Compare exact offers across DigiKey and Mouser

Ask Partuno to compare a specific manufacturer part number and quantity. It
reports unit price, merchandise total, minimum order quantity, order multiple,
purchasable quantity, availability, lead time, and provider status. If one
provider fails or the evidence is incomplete, the result remains partial
instead of inventing a winner.

See the [DigiKey vs Mouser comparison section](../../README.md#digikey-vs-mouser-comparison)
and the [capability reference](../capabilities.md) for the comparison contract.

## Limitations and safety

- You must supply and authorize your own Mouser API credentials.
- Shipping cost and delivery ETA are not returned by the comparison workflow.
- Cart changes require an exact preview and a one-time execution token.
- Partuno never submits a distributor order.
- Inventory, pricing, and account permissions can change between requests.

## Links

- [Partuno source repository](https://github.com/JPMarhefka/Partuno)
- [Partuno on Glama](https://glama.ai/mcp/servers/JPMarhefka/partuno)
- [DigiKey MCP server guide](digikey-mcp-server.md)
- [Electronics BOM MCP server guide](electronics-bom-mcp.md)
