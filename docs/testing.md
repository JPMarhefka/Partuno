# Testing Partuno

The default test suite is offline and safe to run without distributor
credentials. Live provider calls are opt-in because they consume quotas and
may require account permissions.

## Offline regression suite

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q .
```

The tests use mocked distributor responses for provider contracts and verify
MCP tool behavior, REST compatibility, credential handling, normalization,
partial failures, and safety annotations. They should not need live API keys.

## Self-host smoke checks

After deploying an operator-owned instance, verify the health endpoints first:

```text
/health
/mcp-health
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
```

Then connect the MCP client and check, in order:

1. MCP initialization and tool listing succeed.
2. A read-only product search returns structured content and provider status.
3. Product detail and an exact-MPN comparison preserve source evidence.
4. A recommendation evaluates hard requirements as `meets`,
   `does_not_meet`, or `unknown` without treating missing data as a pass.
5. A provider failure or rate limit is reported as partial provider status,
   not as a false empty result.
6. Write-capable workflows show a preview and reject missing confirmation.
7. OAuth reconnects after a restart or redeploy when remote mode is enabled.

Do not use a shared public endpoint for these checks. Use the deployment's own
credentials, rate limits, and account data.

## Live provider tests

Run live tests only with explicit operator approval and within the provider's
terms and quotas. Prefer read-only catalog, detail, comparison, and
recommendation cases. Record provider status, correlation IDs in redacted
form, returned counts, rate-limit metadata, and whether structured content was
present.

Mouser cart mutation tests must be explicitly enabled with the repository's
documented test flag and must stop at cart changes. Partuno does not submit
orders. Never place API keys, OAuth tokens, cookies, or one-time execution
tokens in test fixtures or committed reports.

## Reporting

For a reproducible report, include the case ID, tool, provider, sanitized input
summary, expected behavior, observed result, safety annotation, error state,
and notes about rate limiting. Keep exact credentials, authorization values,
private account identifiers, and customer data out of the report.
