"""Local-first Partuno MCP entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mcp_server import build_mcp_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partuno-mcp",
        description="Run Partuno locally for a desktop MCP client.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for streamable HTTP (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for streamable HTTP (default: 8000).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    server = build_mcp_server(local=True)
    if server is None:  # pragma: no cover - defensive guard for future modes
        raise RuntimeError("Partuno local MCP server could not be initialized")

    try:
        if args.transport == "stdio":
            server.run(transport="stdio", show_banner=False)
            return

        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            path="/mcp",
            show_banner=False,
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    main()
