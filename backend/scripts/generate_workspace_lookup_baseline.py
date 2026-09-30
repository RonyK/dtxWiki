"""Regenerate the tracked Epic-3 Phase E lookup inventory."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

def main() -> int:
    # Imported here rather than at module scope: this module ships inside the
    # wheel (`[tool.setuptools.packages.find] include = ["backend*"]`) while
    # `tests` does not, so an eager import makes the packaged module
    # unimportable -- `import backend.scripts.generate_workspace_lookup_
    # baseline` raises ModuleNotFoundError from any cwd outside this checkout,
    # which the desktop PyInstaller build hits via --collect-submodules=backend.
    # The sys.path bootstrap above still runs first for direct file execution.
    from tests.static.test_workspace_scoped_lookup_gate import (
        BASELINE_PATH,
        workspace_lookup_call_sites,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the tracked baseline with the current inventory",
    )
    parser.add_argument(
        "--allow-additions",
        action="store_true",
        help=(
            "permit new fingerprints while replacing the baseline; reserved "
            "for an explicitly reviewed gate-format migration"
        ),
    )
    args = parser.parse_args()

    inventory = dict(sorted(workspace_lookup_call_sites().items()))
    kinds = Counter()
    for key, count in inventory.items():
        _path, _scope, kind, _target, _fingerprint = key.rsplit("|", 4)
        kinds[kind] += count
    summary = ", ".join(f"{kind}={count}" for kind, count in kinds.most_common())
    if not args.write:
        print(
            f"{len(inventory)} scoped keys, "
            f"{sum(inventory.values())} lookup occurrences"
        )
        print(summary)
        print("pass --write to replace the tracked Phase E baseline")
        return 0

    existing = Counter(
        {
            str(key): int(value)
            for key, value in json.loads(
                BASELINE_PATH.read_text(encoding="utf-8")
            ).items()
        }
    )
    added = Counter(inventory) - existing
    if added and not args.allow_additions:
        preview = list(sorted(added.items()))[:10]
        print(
            "refusing to add new direct lookup fingerprints to the Phase E "
            f"baseline ({sum(added.values())} occurrence(s)); preview={preview}"
        )
        print(
            "use --allow-additions only for an explicitly reviewed gate-format "
            "migration"
        )
        return 1

    BASELINE_PATH.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {len(inventory)} scoped keys / "
        f"{sum(inventory.values())} occurrences to {BASELINE_PATH}"
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
