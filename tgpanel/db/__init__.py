"""db layer: SQLite schema, migrations and repositories.

THE WRITE RULE (phases 4-6: collector, web, bot, scheduler)
-----------------------------------------------------------
Write to the database ONLY through ``await pipeline.db_write(fn, *args)``; reads may use
``db.run``. ``db_write`` takes the pipeline's asyncio write lock, which an apply operation
holds from ``BEGIN IMMEDIATE`` to ``COMMIT``/``ROLLBACK`` on its own connection. A writer
therefore never sees "database is locked", never waits for SQLite's busy timeout, and is
never swallowed into (or rolled back with) an operation transaction. It waits at most
``ApplyTiming.db_write_timeout_s`` (30 s) and then raises ``DbWriteTimeout`` (Russian message).
Operations that change proxy state use ``pipeline.run_operation(mutation, ...)`` instead.
"""
