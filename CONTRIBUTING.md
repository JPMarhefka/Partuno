# Contributing to Partuno

Thank you for helping improve Partuno. Contributions are welcome for provider
adapters, normalization, MCP ergonomics, documentation, tests, and deployment
guides.

## Before opening an issue or pull request

- Search existing issues and pull requests first.
- Use synthetic product, account, and provider data only.
- Never include credentials, bearer tokens, OAuth codes, private URLs, customer
  records, order numbers, quote IDs, MyList IDs, correlation IDs, or personal
  data in an issue, fixture, screenshot, or log.
- Keep provider-specific credentials in environment variables or local secret
  storage. Do not add them to tests or examples.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q .
```

The test suite is offline by default. Live provider calls are opt-in and must
never run in pull-request CI. Do not execute account-changing operations as
part of a normal contribution.

## Pull requests

Keep changes focused and explain:

- what changed and why;
- how the change affects users or MCP clients;
- how it was tested;
- any provider contract, schema, safety, or compatibility implications.

Update the README, deployment documentation, changelog, or security guidance
when behavior changes. Run the same checks used by CI before requesting review.

By intentionally submitting a contribution for inclusion, you agree that it
is provided under the Apache License 2.0, unless you clearly state otherwise
before submission.

## Security issues

Do not open a public issue for a suspected vulnerability or credential
exposure. Follow [`SECURITY.md`](SECURITY.md) instead.
