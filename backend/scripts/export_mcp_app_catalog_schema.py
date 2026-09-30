"""Regenerate the checked-in MCP app catalog JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

from backend.catalog.mcp_apps.schema import catalog_json_schema


def main() -> None:
    target = (
        Path(__file__).resolve().parents[1]
        / "catalog"
        / "mcp_apps"
        / "catalog.schema.json"
    )
    target.write_text(
        json.dumps(catalog_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
