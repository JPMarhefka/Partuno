# Natural-language examples

These examples are written for a user chatting with ChatGPT, Codex, or another
MCP client connected to Partuno. They intentionally describe the engineering
goal in ordinary language rather than naming tools or supplying JSON payloads.

Run one example at a time for a clean screen recording and to keep provider
quotas predictable. Keep the final response focused on evidence and outcomes.

## 1. Choose a component for a 3.3 V design

```text
I’m choosing a dual op amp for a 3.3 V circuit. Find a few LM358-family
options that can operate at 3.3 V, are RoHS compliant, normally stocked, and
available in a quantity of 10. Show the evidence behind each recommendation
and keep uncertain data clearly marked.
```

This is the recommended featured example. It demonstrates cross-provider
search, attribute normalization, hard-requirement evaluation, explicit
uncertainty, quantity-aware offers, and the Pareto shortlist.

## 2. Compare exact offers across distributors

```text
I’m deciding whether to buy the exact onsemi LM358DR2G in a quantity of 10
from DigiKey or Mouser. Compare the available offers, total price, stock,
minimum order quantity, order multiples, and lead time. If there is a tie, say
so clearly. Don’t place an order.
```

This demonstrates strict manufacturer-and-MPN identity, purchasable quantity,
price-break handling, partial provider status, and the difference between a
tie and a false winner.

## 3. Research one component deeply

```text
Please research DigiKey’s LM358DR2GOSCT-ND and give me a concise engineering
brief covering specifications, media, substitutes, alternate packaging,
related products, and any product-change notices. Mention anything that could
not be verified.
```

This shows the lean product-research path plus opt-in enrichment while keeping
partial enrichment failures visible.

## 4. Review a small BOM for sourcing risk

```text
Review this small BOM for sourcing risk and availability:

U1 — onsemi LM358DR2G, quantity 10
U2 — onsemi LM358DR2G, quantity 10

Check pricing, stock, lifecycle, compliance, lead time, alternate packaging,
and substitutes. Summarize the important risks without changing any account
or purchasing data.
```

This demonstrates BOM-level stock, lifecycle, compliance, pricing, lead-time,
substitute, and packaging analysis.

## 5. Preview an account change without applying it

```text
Show me what would change if I added 10 LM358DR2GOSCT-ND parts to my DigiKey
list. Give me a dry-run preview only. Do not apply the change, rename the list,
delete anything, or modify my account.
```

This demonstrates the safety boundary: read-only inspection and a proposed
diff can be shown without executing a mutation. Redact list IDs, account IDs,
customer references, and private project names before publishing a recording.

## Suggested recording layout

For the README, use the first example as the featured story. A short
sanitized recording can show the prompt, the tool activity, and the final
recommendation. Additional recordings can be stored under `docs/assets/` with
names such as:

- `featured-recommendation.gif`
- `exact-offer-comparison.gif`
- `product-research.gif`
- `bom-risk-review.gif`
- `safety-preview.gif`

Keep original MP4 or MOV files outside the repository unless they are also
sanitized and intentionally published. Never include credentials, OAuth
codes, bearer tokens, private URLs, customer data, or reusable mutation
tokens.
