"""RealShellTools.migrate_database must work with a real interpreter (PYTHONPATH, not -I)."""

from __future__ import annotations

import sys
from pathlib import Path

from tgpanel.system.tools import RealShellTools

REPO = str(Path(__file__).resolve().parents[2])


async def test_migrate_database_runs_module_from_repo(tmp_path: Path) -> None:
    db = tmp_path / "state" / "tgpanel.db"
    db.parent.mkdir(mode=0o700)
    out = await RealShellTools().migrate_database(sys.executable, REPO, str(db))
    assert db.exists()
    assert out is not None
