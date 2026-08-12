# Local MCP deployment

Partuno's default distribution path is a local MCP process. It does not need
Render, Azure, a database, Redis, or a Partuno account. Provider credentials
stay in the operator's environment.

## Quick start

From a checkout of the repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
set -a
source .env
set +a
python -m partuno
```

The default transport is MCP stdio, which is the safest path for desktop MCP
clients. Configure the client to launch `python -m partuno` from this
directory. The process does not open a listening network socket.

For clients that require HTTP, use the loopback-only transport:

```bash
python -m partuno --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. The default bind host must
remain loopback unless the operator has deliberately placed the server behind
an authenticated and encrypted reverse proxy.

## Credentials

Set only credentials obtained by the operator from the relevant provider. A
local DigiKey MCP process accepts an optional `DIGIKEY_ACCESS_TOKEN` for
catalog and account calls; it is never returned by a tool or written to logs.
Mouser search and account capabilities use their corresponding API-key
variables in `.env.example`.

Do not put credentials in MCP tool arguments, source control, screenshots, or
client configuration files that are shared with other people.

## Installed command

After installing the project with `python -m pip install -e .`, the equivalent
command is:

```bash
partuno-mcp
```

Run `partuno-mcp --help` to see the transport options.
