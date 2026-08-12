# Partuno

[![CI](https://github.com/JPMarhefka/partuno/actions/workflows/ci.yml/badge.svg)](https://github.com/JPMarhefka/partuno/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/JPMarhefka/partuno)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)](https://www.python.org/)

## Local-first component intelligence for MCP

Partuno is an open-source Model Context Protocol (MCP) server for electronic
component research, BOM analysis, sourcing comparisons, and carefully bounded
distributor workflows. It connects AI assistants to operator-owned DigiKey and
Mouser credentials while returning evidence, normalized attributes, and
explicit uncertainty.

Partuno was initially developed as a native MCP server. Native MCP is the
recommended connection because it works as an app in ordinary ChatGPT
conversations and exposes richer tool annotations. Existing Custom GPT Action
endpoints remain available for backward compatibility, including
`/action-openapi.json`.

> Partuno does not provide a shared public server or shared distributor
> credentials. The Render configuration is an optional reference deployment
> for an operator's own single-user instance.

## Start here

### Run the local MCP server

The default path is local MCP over stdio. Provider credentials remain in your
environment and the process does not open a public network port.

```bash
git clone https://github.com/JPMarhefka/partuno.git
cd partuno
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m partuno
```

Then configure your MCP client to launch `python -m partuno` from the checkout.
See [MCP setup](MCP_SETUP.md) and the [local deployment guide](docs/deployment/local.md).

### Self-host a remote MCP server

If your client requires a reachable HTTPS endpoint, deploy your own instance
with your own provider accounts, OAuth application, signing key, rate limits,
and hosting budget. Start with the [Render reference deployment](docs/deployment/render.md),
or use the [Docker](docs/deployment/docker.md) and [Azure](docs/deployment/azure.md)
guides. There is no shared Partuno endpoint intended for public use.

### Use the existing Action compatibility surface

Custom GPT Actions are retained for existing integrations. The Action/OpenAPI
surface is not the primary architecture; use native MCP for new connections.
The Action setup details are documented in [MCP setup](MCP_SETUP.md).

## What Partuno does

- Searches and inspects electronic components across DigiKey and Mouser.
- Normalizes manufacturer part numbers, units, compliance values, and common
  distributor-specific attribute names.
- Compares exact component offers at a requested quantity, including price
  breaks, minimum order quantities, order multiples, availability, and partial
  provider failures.
- Recommends components against hard requirements while preserving
  `meets`, `does_not_meet`, and `unknown` evidence states.
- Analyzes BOMs for stock, lifecycle, compliance, pricing, tariffs, lead time,
  and substitute candidates.
- Supports bounded account and project workflows such as read-only order
  history, list and quote previews, receiving reconciliation, and carefully
  confirmed mutations where the provider supports them.

See the [capability reference](docs/capabilities.md) for the current scope and
the safety boundary for each workflow.

## What Partuno does not do

- It does not supply DigiKey or Mouser credentials.
- It does not create a shared multi-tenant credential pool.
- It does not silently turn unknown product data into a pass.
- It does not submit distributor orders.
- It does not bypass provider quotas, terms, authentication, or rate limits.

## Example prompts

Once the MCP server is connected, try prompts such as:

```text
Find three normally stocked, RoHS-compliant LM358-family parts and show the
manufacturer MPN, package, voltage range, stock, and source evidence.

Compare the exact onsemi LM358DR2G at a quantity of 10 across DigiKey and
Mouser. Show each purchasable offer and explain any tie or missing data.

Recommend a dual op amp for a 3.3 V design. Treat supply range and RoHS as
hard requirements, preserve unknown values as unknown, and show the evidence.
```

## Documentation

| Topic | Guide |
| --- | --- |
| Capabilities and safety boundaries | [Capability reference](docs/capabilities.md) |
| Native MCP connection | [MCP setup](MCP_SETUP.md) |
| Local stdio or loopback HTTP | [Local deployment](docs/deployment/local.md) |
| Docker deployment | [Docker deployment](docs/deployment/docker.md) |
| Operator-owned Render deployment | [Render reference](docs/deployment/render.md) |
| Azure deployment | [Azure deployment](docs/deployment/azure.md) |
| Security and credential handling | [Security guide](docs/security.md) |
| Offline and live validation | [Testing guide](docs/testing.md) and [validation notes](VALIDATION.md) |
| Demo recording and GIF guidance | [Demo media guide](docs/demos.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security reports | [SECURITY.md](SECURITY.md) |
| Release history | [CHANGELOG.md](CHANGELOG.md) |

## Demo media

The repository includes a small representative preview:

![Partuno representative demo](docs/assets/partuno-demo.gif)

For a more realistic demonstration, record short clips from your own deployed
Render instance after redacting credentials, OAuth state, private URLs, account
identifiers, and customer data. The [demo media guide](docs/demos.md) includes
recommended clips, Mac capture steps, GIF settings, and privacy checks. Keep
the original MP4 or MOV alongside a lightweight GIF preview when possible.

## Deployment and credential model

| Mode | Best for | Credential ownership |
| --- | --- | --- |
| Local stdio | Desktop MCP clients and personal development | Operator's local environment |
| Loopback HTTP | Clients that cannot launch stdio | Operator's local environment |
| Remote HTTPS | A self-hosted ChatGPT/Codex connection | Operator's own deployment and accounts |

Remote mode adds OAuth and HTTPS because a hosted MCP endpoint must authenticate
the client and protect upstream access. It is optional; local mode remains the
default and does not require Partuno OAuth.

## Project status

Partuno 4.0.0 is an active open-source release. The test suite covers offline
provider contracts, MCP tool behavior, REST compatibility, credential safety,
normalization, and the multi-distributor workflows. Live provider calls are
opt-in because they require operator credentials and are subject to provider
quotas.

See [testing](docs/testing.md) before running live smoke tests.

## License and independence

Partuno is released under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE)
for project attribution and independence statements. Partuno is not affiliated
with, endorsed by, or sponsored by DigiKey, Mouser, or any manufacturer or
distributor named in the project. Their names and marks identify integrations
only.
