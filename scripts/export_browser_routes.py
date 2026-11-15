"""Export the shared workspace browser path contract for the frontend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.browser_routes import render_typescript  # noqa: E402

DESTINATION = (
    ROOT / "frontend/src/shared/lib/browserRoutes.gen.ts"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render_typescript()
    if args.check:
        if not DESTINATION.exists() or DESTINATION.read_text() != content:
            raise SystemExit(
                "Browser route contract is stale; run scripts/export_browser_routes.py"
            )
    else:
        DESTINATION.write_text(content)


if __name__ == "__main__":
    main()
