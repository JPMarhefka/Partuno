# Local MCP deployment

Partuno's default distribution path is a local MCP process. It does not need
Render, Azure, a database, Redis, or a Partuno account. Provider credentials
stay in the operator's environment.

## Quick start with the published package

For normal use, launch the published PyPI package with `uvx`:

```bash
uvx --from partuno partuno-mcp
```

The default transport is MCP stdio, which is the safest path for desktop MCP
clients. Configure the client to launch `uvx` with `--from partuno partuno-mcp`.
The process does not open a listening network socket.

For an exact reproducible release, pin the package version:

```bash
uvx --from 'partuno==4.0.1' partuno-mcp
```

For clients that require HTTP, use the loopback-only transport:

```bash
uvx --from partuno partuno-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. The default bind host must
remain loopback unless the operator has deliberately placed the server behind
an authenticated and encrypted reverse proxy.

## Credentials

Set only credentials obtained by the operator from the relevant provider. A
local DigiKey MCP process accepts an optional `DIGIKEY_ACCESS_TOKEN` for
catalog and account calls; it is never returned by a tool or written to logs.
Mouser search and account capabilities use `MOUSER_SEARCH_API_KEY` and
`MOUSER_ACCOUNT_API_KEY`. The complete variable reference is in the
[`.env.example`](../../.env.example) file.

Do not put credentials in MCP tool arguments, source control, screenshots, or
client configuration files that are shared with other people.

## Develop from source

Use a repository checkout when contributing, testing unreleased changes, or
needing the example environment file:

```bash
git clone https://github.com/JPMarhefka/partuno.git
cd partuno
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
set -a
source .env
set +a
python -m partuno
```

The published package exposes the same `partuno-mcp` command. Run
`partuno-mcp --help` to see the transport options.
