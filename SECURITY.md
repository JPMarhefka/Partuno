# Security Policy

Partuno connects AI clients to distributor APIs using credentials supplied by
the operator. A credential, token, customer record, BOM, order identifier, or
other account-specific value must never be posted in a GitHub issue, pull
request, discussion, demo asset, or public log.

## Reporting a vulnerability

Please report security vulnerabilities privately through GitHub's private
vulnerability-reporting or Security Advisory feature. If private reporting is
not available, use the maintainer contact method [email](mailto:contact@jpmarhefka.com).

Do not open a public issue for a suspected credential leak or an exploitable
security defect. If a credential may have been exposed, revoke or rotate it
with the relevant provider immediately, then report the exposure privately.

Include, where safe:

- A concise description and impact assessment.
- The affected version or commit.
- Reproduction steps using synthetic data only.
- Logs or screenshots with credentials, tokens, account identifiers, and
  personal data removed.

## Security boundaries

- Partuno uses user-owned, provider-specific credentials; it does not provide
  shared distributor credentials.
- Credentials and OAuth tokens must be supplied at runtime and must not be
  committed, logged, returned in tool output, or included in test fixtures.
- Partuno does not submit purchase orders.
- Consequential cart, list, quote, or account mutations require explicit
  confirmation and must not be automatically retried.
- Users are responsible for provider terms, quotas, access permissions, and
  the security of any remote host they operate.

The detailed credential and deployment model is documented in
[`docs/security.md`](docs/security.md), with deployment-specific guidance in
[`docs/deployment/`](docs/deployment/).

## Supported releases

The latest tagged release is the supported release line. Development builds
may change provider behavior, MCP tool schemas, and deployment configuration
without notice; pin a release tag for reproducible self-hosting.
