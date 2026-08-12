# Partuno Native MCP Setup

Partuno was initially developed as a native MCP server. It exposes an optional
remote Streamable HTTP MCP server at `/mcp` while preserving the existing REST
and OpenAPI endpoints for backward compatibility. This guide covers
`PARTUNO_MODE=remote_single_user`; local mode is the default for self-hosted
operation and does not require this OAuth proxy.

## Render environment variables

Add these protected environment variables to the existing Render service:

```text
PARTUNO_MODE=remote_single_user
DIGIKEY_CLIENT_ID=your_production_client_id
DIGIKEY_CLIENT_SECRET=your_production_client_secret
MOUSER_SEARCH_API_KEY=your_search_api_key
MOUSER_ACCOUNT_API_KEY=your_cart_and_order_history_api_key
MCP_BASE_URL=https://YOUR-SERVICE.onrender.com
MCP_JWT_SIGNING_KEY=a-long-random-secret
```

`MCP_BASE_URL` must be the public Render origin without a trailing slash.

The DigiKey client secret is required in `remote_single_user` mode for the MCP
OAuth proxy to exchange and refresh authorization codes. All listed values are
operator-owned credentials for this deployment; Partuno does not provide
shared credentials. Mouser keys are never sent through ChatGPT tool input.
Never commit any credential to GitHub.

Generate `MCP_JWT_SIGNING_KEY` once with `openssl rand -base64 48` and store it
as a Render secret. It keeps MCP-issued tokens valid across ordinary worker
restarts. This reference deployment deliberately uses FastMCP's local
ephemeral OAuth storage: after a Render Free spin-down, restart, or redeploy,
reconnect the plugin and sign in again. Do not configure multiple instances
with this mode; it is not a shared multi-tenant credential service.

## DigiKey callback URL

Edit the DigiKey production application and set its callback URL to:

```text
https://YOUR-SERVICE.onrender.com/auth/callback
```

It must match exactly.

## Verify after deployment

Open:

```text
https://YOUR-SERVICE.onrender.com/mcp-health
```

Expected result:

```json
{
  "status": "ok",
  "version": "4.0.1",
  "mcp": {
    "enabled": true,
    "url": "https://YOUR-SERVICE.onrender.com/mcp",
    "callback_url": "https://YOUR-SERVICE.onrender.com/auth/callback",
    "storage": "ephemeral",
    "stable_signing_key_configured": true
  }
}
```

An unauthenticated request to `/mcp` should return HTTP 401 and advertise OAuth protected-resource metadata. That is expected.

## Add it in ChatGPT

1. Open **Settings > Apps > Developer mode > New Plugin**.
2. Name it `Partuno`.
3. Set the description to `Provider-neutral DigiKey and Mouser MCP server for electronic component research, BOM analysis, sourcing comparison, and safe distributor workflows.`
4. Choose **Server URL**.
5. Enter `https://YOUR-SERVICE.onrender.com/mcp`.
6. Choose **OAuth**.
7. Open Advanced OAuth settings and review the discovered values.
8. Accept the unverified-server warning.
9. Create the plugin.
10. Complete the FastMCP consent screen and DigiKey sign-in.

ChatGPT should discover 50 MCP tools. Product research starts with lean search
and product-detail tools; call `research_product` with explicit enrichment
flags only when media, replacements, PCNs, or related parts are needed.
Read-only tools are annotated as safe reads. MyList and quote changes are
annotated as write or destructive operations and require `confirm: true` in
their tool input. Mouser Cart mutations require a preview followed by the
returned one-time execution token. No tool can submit an order.

## Add it in Codex

Use the same remote server and sign-in flow:

```bash
codex mcp add partuno --url https://YOUR-SERVICE.onrender.com/mcp
codex mcp login partuno
```

Complete the FastMCP consent screen and DigiKey sign-in in the browser Codex
opens. The connection can then be enabled from Codex's MCP settings. If Render
has slept or redeployed, run the login command again.

## Render storage note

Render Free instances spin down after 15 minutes without traffic and can take
about a minute to wake. Their filesystem is ephemeral, so OAuth state and
upstream tokens are lost on a spin-down, restart, or redeploy. This is suitable
for a personal or reference deployment because reconnecting is acceptable. Do
not treat this single-user configuration as a public multi-tenant service; use
separate operator-owned deployments until a separately reviewed multi-user
credential architecture exists.

## Backward compatibility

The existing Custom GPT Action endpoints remain available, including
`/action-openapi.json`. Native MCP is the recommended connection because it
works as an app in normal ChatGPT conversations and exposes richer MCP tool
annotations.
