"""Helper: a server where one profile was imported and one user was created in the panel."""

from __future__ import annotations

import asyncio

from tests.ops.conftest import FAST, Ops
from tgpanel.services.api import NewUser
from tgpanel.services.container import build_context


def import_two_and_create_one(ops: Ops) -> None:
    async def go() -> None:
        ctx = build_context(ops.fake, ops.db, config=FAST)
        try:
            await ctx.start(recover=False)
            preview = await ctx.importer.preview()
            res = await ctx.importer.confirm(preview, actor="test")
            assert res.ok, res.error
            made = await ctx.users.create([NewUser(name="fresh")], "test")
            assert made.ok, made.error
        finally:
            ctx.close()

    asyncio.run(go())
