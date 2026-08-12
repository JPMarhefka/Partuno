# Docker and Compose deployment

Docker is the reproducible self-hosting path for operators who want a local
HTTP MCP endpoint or a private reference deployment. Credentials are supplied
at runtime through `.env`; they are not copied into the image.

## Local Compose quick start

```bash
cp .env.example .env
# Edit .env with only your own provider credentials.
docker compose up --build
```

The Compose file binds only to loopback and serves the combined API/MCP
process at:

- Health: `http://127.0.0.1:8000/health`
- MCP: `http://127.0.0.1:8000/mcp`

Set `PARTUNO_PORT` if port 8000 is already in use. With the default
`PARTUNO_MODE=local`, the MCP endpoint has no hosted OAuth proxy; access is
limited to the local machine and provider credentials remain operator-owned.

Verify the container is healthy:

```bash
curl http://127.0.0.1:8000/health
docker compose ps
```

Stop it with `docker compose down`. The service has no database or Redis
dependency and does not persist provider tokens in the container.

## Security boundaries

- Keep `.env` outside source control and do not bake credentials into a custom
  image layer.
- Keep the host binding on `127.0.0.1` unless an authenticated TLS reverse
  proxy is intentionally configured.
- Use `PARTUNO_MODE=remote_single_user` only when the operator has a public
  HTTPS origin, a stable `MCP_JWT_SIGNING_KEY`, and a DigiKey OAuth client
  authorized for that deployment.
- A public container increases credential and attack-surface risk compared
  with stdio or loopback-only local mode.
