# Third-party notices

Partuno's original source and documentation are released under the Apache
License 2.0. Partuno also depends on independently licensed open-source
packages. Their names and licenses are listed here for attribution and review;
the authoritative license text remains with each package distribution.

## Direct runtime dependencies

| Package | Version | License | Project |
| --- | --- | --- | --- |
| FastAPI | 0.139.2 | MIT | <https://github.com/fastapi/fastapi> |
| Uvicorn | 0.51.0 | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| Requests | 2.34.2 | Apache-2.0 | <https://github.com/psf/requests> |
| Pydantic | 2.13.4 | MIT | <https://github.com/pydantic/pydantic> |
| HTTPX | 0.28.1 | BSD-3-Clause | <https://github.com/encode/httpx> |
| FastMCP | 3.4.4 | Apache-2.0 | <https://github.com/jlowin/fastmcp> |

Transitive dependencies are installed from the pinned dependency set in
`requirements.txt` and `requirements-dev.txt`. Their package metadata and
license files remain authoritative; redistributors should preserve those
notices when packaging Partuno with dependencies.

Partuno does not include DigiKey or Mouser logos, provider documentation, or
provider-owned catalog data in this repository. Provider names are used only
to identify supported integrations; see [`NOTICE`](NOTICE).
