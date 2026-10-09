"""Runtime directories the service needs before its first apply."""

from __future__ import annotations

from tgpanel.apply.config import ApplyPaths
from tgpanel.system.ops import SystemOps


async def ensure_runtime_dirs(ops: SystemOps, paths: ApplyPaths) -> None:
    """Create /etc/tgpanel (+ /mtproxy), the backups and the state directories, all 0700."""
    for directory in (paths.tgpanel_dir, paths.pools_dir, paths.backups_dir, paths.state_dir):
        await ops.ensure_dir(directory, 0o700, "root", "root")
