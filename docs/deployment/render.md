# Render reference deployment

Render is an optional maintainer/reference deployment. It is not required for
local use and must not be configured with a shared credential pool for
unrelated users.

## Configuration

Use the repository `render.yaml` or create a Docker web service with:

```text
PARTUNO_MODE=remote_single_user
DIGIKEY_CLIENT_ID=<operator-owned DigiKey OAuth client ID>
DIGIKEY_CLIENT_SECRET=<protected DigiKey OAuth client secret>
MCP_BASE_URL=https://<service>.onrender.com
MCP_JWT_SIGNING_KEY=<long random protected signing key>
MOUSER_SEARCH_API_KEY=<optional operator-owned key>
MOUSER_ACCOUNT_API_KEY=<optional operator-owned key>
```

Keep `MCP_BASE_URL` as the HTTPS origin without a path or trailing slash. Do
not place these values in GitHub, Docker layers, tool arguments, or logs.

## Verification

After deployment, check:

```text
https://<service>.onrender.com/health
https://<service>.onrender.com/mcp-health
https://<service>.onrender.com/.well-known/oauth-protected-resource/mcp
https://<service>.onrender.com/.well-known/oauth-authorization-server
```

The native MCP URL is:

```text
https://<service>.onrender.com/mcp
```

In ChatGPT, use OAuth and prefer Dynamic Client Registration when offered.
The existing Custom GPT Action/OpenAPI endpoints remain available for
backward compatibility, but native MCP is the recommended connection.

Render free services can sleep or restart. Treat reconnecting OAuth after a
deploy or wake-up as expected behavior, and never use the reference service as
a substitute for local BYOK operation.
