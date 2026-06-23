"""Durable LangGraph checkpointer backed by the pgvector Postgres.

Sessions (threads) and their accumulated state survive process/pod restarts and are
shared across replicas. Reuses DATABASE_URL — no extra infrastructure. The checkpoint
tables (checkpoints, checkpoint_writes, ...) are created by PostgresSaver.setup() and
live alongside the registry tables in the same database.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import config


@lru_cache(maxsize=1)
def get_checkpointer() -> PostgresSaver:
    pool = ConnectionPool(
        conninfo=config.database_url,
        max_size=10,
        open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    saver = PostgresSaver(pool)
    saver.setup()  # idempotent: creates/migrates checkpoint tables
    return saver
