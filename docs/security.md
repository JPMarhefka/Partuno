# Partuno Security Model

Partuno is designed as a local-first, self-hostable MCP server. The operator
controls the host, provider accounts, credentials, and network exposure. A
future hosted reference deployment must not turn into a shared credential
gateway for unrelated users.

## Credential ownership and loading

The public design uses provider-specific, user-owned credentials:

- **DigiKey:** the operator's developer application credentials and
  user-scoped OAuth authorization.
- **Mouser:** the operator's Search API key and, where required, separate
  Account API key.

Credentials should be injected through the runtime environment or a local
secure store. They must not be baked into a Docker image, committed to Git,
placed in MCP tool arguments, or emitted in logs and error responses.

The planned v4 credential abstraction keeps provider and purpose separate so
that missing credentials degrade only the related capability. For example,
missing a Mouser Account key must disable account history/cart tools without
disabling Mouser Search, and missing DigiKey credentials must not disable
Mouser. The first public release does not require a hosted multi-tenant
credential vault.

## Runtime and deployment modes

### Local mode

Local execution is the default security posture. Bind network transports to a
loopback interface unless the operator deliberately configures another
interface, and keep credentials on the operator's machine.

### Remote single-user mode

A self-hosted remote instance may run on Render, Azure, a VPS, or a home server
when the operator controls the host and its secrets. Use TLS and an
authenticated reverse proxy for internet exposure. Do not expose an
unauthenticated MCP endpoint or place provider credentials in client-visible
configuration.

### Maintainer reference instance

JP's hosted instance may serve as a demo or development reference using only
credentials JP is authorized to use. It is not a shared Partuno credential
service.

## Tool and data safety

- Partuno does not submit orders or purchase orders.
- Cart, list, quote, and other consequential mutations require explicit user
  confirmation.
- Read operations may use bounded retries where safe; mutations must not be
  automatically retried.
- Error responses should preserve useful status, retryability, correlation, and
  rate-limit metadata without exposing authorization headers, API keys, OAuth
  tokens, or refresh tokens.
- Sample BOMs and demos must use generic components and synthetic account data.
- Correlation IDs, MyList IDs, quote IDs, order numbers, addresses, emails, and
  other account-specific metadata must be removed from public reports and
  screenshots.

## Contributor requirements

Contributors must:

1. Use synthetic credentials and provider responses in tests.
2. Review diffs for secrets and account-specific metadata before opening a PR.
3. Redact logs and fixtures before committing them.
4. Avoid live provider writes in pull-request CI and untrusted forks.
5. Report suspected exposures privately rather than in public issues.

The repository should enforce these rules with secret scanning, dependency
scanning, and protected maintainer-only live-test environments before the first
public release.
