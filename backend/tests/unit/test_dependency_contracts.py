"""The MCP SDK requirement: a security floor, and never the v2 rewrite.

mcp 1.28.1 is the first release with fixes for every SDK advisory this server
can reach (session/credential binding, task handlers, WebSocket Host/Origin
validation), so the requirement must not admit anything older. mcp 2 rebuilt
the low-level server API that backend/api/mcp_http.py drives, so it must not
admit 2.x either. The exact pin this test used to hold blocked every 1.x
security release.
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[3]


def _mcp_requirement() -> Requirement:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(spec) for spec in project["project"]["dependencies"]]
    (mcp,) = [requirement for requirement in requirements if requirement.name == "mcp"]
    return mcp


def test_mcp_sdk_dependency_has_a_security_floor_and_stays_on_v1():
    specifier = _mcp_requirement().specifier

    assert not specifier.contains("1.28.0")
    assert specifier.contains("1.28.1")
    # A later 1.x security release must stay installable.
    assert specifier.contains("1.99.0")
    assert not specifier.contains("2.0.0")
