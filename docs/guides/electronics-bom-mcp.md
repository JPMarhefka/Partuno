# Electronics BOM MCP Server for AI Sourcing | Partuno

Partuno is an **electronics BOM MCP server** for analyzing component lists,
checking sourcing risk, and comparing distributor offers with evidence. It
connects an AI assistant to operator-owned DigiKey and Mouser integrations while
keeping missing data, provider failures, and safety boundaries explicit.

## What the electronics BOM MCP server does

Given a BOM and requested quantities, Partuno can:

- Consolidate duplicate part numbers and preserve customer references.
- Resolve exact product details and calculate current estimated merchandise cost.
- Check stock coverage, minimum order quantities, price breaks, lifecycle,
  manufacturer lead time, RoHS, REACH, MSL, ECCN, and HTS data.
- Flag last-buy dates, product-change notices, shortages, tariff-bearing options,
  and Marketplace products.
- Retrieve substitutes and alternate packaging for risky or unavailable parts.
- Compare exact DigiKey and Mouser offers when both provider integrations are
  available.

<p align="center">
  <img src="../assets/demos/bom-risk-review.gif" alt="Partuno electronics BOM MCP server reviewing sourcing risk" width="900">
</p>

## Example request

```text
Review this small BOM for sourcing risk and availability:

U1 — onsemi LM358DR2G, quantity 10
U2 — onsemi LM358DR2G, quantity 10

Check pricing, stock, lifecycle, compliance, lead time, alternate packaging,
and substitutes. Summarize the important risks without changing any account or
purchasing data.
```

See [more natural-language examples](../examples/README.md) for component
recommendation, exact offer comparison, product research, and safe account
previews.

## Setup

The default path is local MCP over stdio:

```bash
git clone https://github.com/JPMarhefka/partuno.git
cd partuno
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m partuno
```

Configure the MCP client to launch `python -m partuno` from the checkout. The
[native MCP setup guide](../../MCP_SETUP.md) also covers loopback HTTP and an
operator-owned remote deployment.

## Supported MCP workflows

The main BOM and sourcing tools are `analyze_bom`, `optimize_bom_pricing`,
`audit_lifecycle`, `recommend_components`, and `compare_component_offers`.
They combine provider responses but preserve which provider supplied each
signal and whether a requirement is `meets`, `does_not_meet`, or `unknown`.

## Limitations and safety

- A BOM review is evidence gathering, not an engineering approval or purchasing
  decision.
- Unknown or conflicting attributes are reported rather than guessed.
- Stock, pricing, lifecycle, and lead-time values are time-sensitive provider
  data.
- Shipping, tax, and delivery ETA are not silently estimated.
- Account, list, quote, and Cart changes require their explicit confirmation
  boundary; Partuno cannot submit an order.

## Links

- [Partuno source repository](https://github.com/JPMarhefka/Partuno)
- [Partuno on Glama](https://glama.ai/mcp/servers/JPMarhefka/partuno)
- [DigiKey MCP server guide](digikey-mcp-server.md)
- [Mouser MCP server guide](mouser-mcp-server.md)
