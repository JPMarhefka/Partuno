# Azure container deployment

Partuno can run as a single container on Azure Container Apps, App Service for
Containers, or another Azure-managed container host. The application does not
require PostgreSQL, Redis, or a Partuno account.

## Required shape

1. Build the repository `Dockerfile`.
2. Store provider credentials and `MCP_JWT_SIGNING_KEY` in Azure-managed
   secrets, not in the image or repository.
3. Set `PARTUNO_MODE=remote_single_user` for an internet-accessible MCP host.
4. Set `MCP_BASE_URL` to the final HTTPS origin without `/mcp`.
5. Route the platform's HTTPS ingress to container port `8000`.
6. Configure the platform health probe for `/health`.

The MCP endpoint is `/mcp`. Terminate TLS at Azure ingress or an equivalent
reverse proxy before exposing the service publicly. Do not bind a public
container without authentication and HTTPS.

## Runtime configuration

Use the same provider-specific variables documented in
[`render.md`](render.md). Mouser Search and Account keys are independent, and
missing account credentials must not be replaced with a shared key.

For private/local use, keep ingress internal and use
`PARTUNO_MODE=local`; the stdio launcher remains the simpler and safer local
client path. Public remote hosting carries more credential and attack-surface
risk than local mode.
