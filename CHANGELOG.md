# Changelog

## 4.0.1

- Made the published PyPI package and official MCP Registry the primary local
  installation path in the README and deployment guides.
- Added `uvx` and pinned-release examples for stdio and loopback HTTP use.
- Clarified that source checkout setup is for contributors and unreleased
  changes, while preserving the operator-owned credential model.

## 4.0.0

- Added the local-first MCP launcher, Docker/Compose self-hosting path, and
  deployment guides for local, Docker, Render, and Azure environments.
- Added provider-attribute normalization for DigiKey voltage spans and RoHS
  evidence, plus the corrected strict manufacturer-MPN validation fixture.
- Rebranded the project as Partuno, an open-source MCP server for electronic
  component research, BOM analysis, sourcing optimization, and safe distributor
  workflows.
- Made local/self-hosted operation and operator-owned provider credentials the
  primary public deployment model, with `remote_single_user` as an optional
  reference mode.
- Updated application, MCP, OpenAPI, setup, and user-agent metadata to use the
  Partuno 4.0 identity.
- Added explicit independence language: Partuno is not affiliated with,
  endorsed by, or sponsored by DigiKey, Mouser, or other listed distributors.
- Fixed the container deployment manifest to include the runtime credential
  module required by the FastAPI application at startup.
- Set an explicit writable home directory for the non-root container user so
  FastMCP's ephemeral OAuth proxy storage can initialize on hosted deployments.
- Normalized DigiKey voltage-span minimum/maximum aliases and compliance values
  such as `ROHS3 Compliant` for requirements-aware recommendations.
- Added Apache-2.0 licensing, contributor guidance, security policy, CI, and
  public-repository issue templates.

## 3.0.0

- Added Mouser Search, read-only Order History, and the complete Cart API.
- Added strict DigiKey/Mouser offer comparison and evidence-based project
  recommendations with deterministic Pareto shortlists.
- Added environment-backed distributor credential interfaces for future
  tenant-aware storage.
- Added per-provider error metadata, Mouser key redaction, local rate budgets,
  and partial-source behavior with no incomplete winner.
- Added state-bound, expiring, one-time Cart preview tokens; Cart writes are
  never retried and order submission remains unavailable.
- Preserved all existing DigiKey routes and MCP tool names.
- Stopped retrying detected DigiKey daily-limit 429 responses, preserved their
  structured rate metadata, and added a short successful-PCN response cache.
- Added header/body/null Mouser correlation tracing without synthesizing IDs.
- Split DigiKey pricing-option availability from variation/package stock in
  combined offers and kept region-restricted Mouser offers unpurchasable.
- Added a corrected stress-test manifest separating current pricing from
  quantity-15 pricing.

## 2.1.1

- Added sanitized correlation, rate-limit, and per-attempt diagnostics to failed
  and retried DigiKey reads while keeping writes non-retrying.
- Made recommendation filters optional and added one explicit-filter
  compatibility retry for DigiKey 404/500 responses.
- Bounded KeywordSearch conformance fallback scans and exposed completeness
  metadata without rewriting upstream stock.
- Added raw and effective MOQ fields, including first-price-break recovery when
  DigiKey reports a raw MOQ of zero.
- Added compact MyList part views, normalized access diagnostics, and contextual
  deleted-or-inaccessible list errors while retaining raw fields and statuses.
- Added PCN description-date parsing and mismatch warnings.
- Sent substitution limits upstream and retained a defensive local cap with
  returned and available counts.
- Preserved partial BOM optimization and research results when one enrichment
  or alternate package fails.
- Added OpenAPI/MCP safety regressions proving operation IDs remain unique,
  write tools require confirmation, and no order-placement API is exposed.

## 2.0.0

- Added full parametric product search and pagination.
- Added marketplace, tariff, minimum-stock, status, package, series, and field filters.
- Added bulk BOM analysis and duplicate consolidation.
- Added quantity and packaging optimization, including BetterValue, MOQ, and DigiReel.
- Added lifecycle and PCN audit workflows.
- Added MyList dry-run diff and confirmed synchronization.
- Added Quote V4 list, create, read, product retrieval, batch product addition, and BOM or MyList quote creation.
- Added Reference API associated-account lookup.
- Added all four Barcode API decoding modes.
- Added batch receiving totals and comparison against a MyList.
- Added packing-list lookup by invoice, sales order, or purchase order.
- Added safe read retries, rate-limit metadata, concurrency controls, and partial-progress reporting.
- Added a compact 30-operation GPT Action schema and a complete 44-operation debugging schema.
