"""pgvector registry access: schema bootstrap, idempotent upsert, top-k retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from .config import config

_SCHEMA_SQL = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def connect() -> psycopg.Connection:
    conn = psycopg.connect(config.database_url)
    # The `vector` type must exist before psycopg can register its adapter.
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA_SQL.read_text())
    conn.commit()


@dataclass
class Candidate:
    microservice_name: str
    http_method: str
    endpoint_path: str
    operation_id: str | None
    semantic_description: str
    raw_openapi_json: dict
    distance: float


def upsert_operation(
    conn: psycopg.Connection,
    *,
    project_id: str,
    microservice_name: str,
    http_method: str,
    endpoint_path: str,
    operation_id: str | None,
    raw_openapi_json: dict,
    semantic_description: str,
    embedding: list[float],
) -> None:
    conn.execute(
        """
        INSERT INTO api_endpoint_registry
            (project_id, microservice_name, http_method, endpoint_path,
             operation_id, raw_openapi_json, semantic_description, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (project_id, microservice_name, http_method, endpoint_path)
        DO UPDATE SET
            operation_id = EXCLUDED.operation_id,
            raw_openapi_json = EXCLUDED.raw_openapi_json,
            semantic_description = EXCLUDED.semantic_description,
            embedding = EXCLUDED.embedding
        """,
        (
            project_id,
            microservice_name,
            http_method.upper(),
            endpoint_path,
            operation_id,
            json.dumps(raw_openapi_json),
            semantic_description,
            embedding,
        ),
    )


def search(
    conn: psycopg.Connection,
    query_embedding: list[float],
    *,
    project_id: str,
    k: int,
) -> list[Candidate]:
    """Top-k operations by cosine distance, scoped to a project (MAUI guide §5)."""
    rows = conn.execute(
        """
        SELECT microservice_name, http_method, endpoint_path, operation_id,
               semantic_description, raw_openapi_json,
               embedding <=> %s AS distance
        FROM api_endpoint_registry
        WHERE project_id = %s
        ORDER BY embedding <=> %s
        LIMIT %s
        """,
        (Vector(query_embedding), project_id, Vector(query_embedding), k),
    ).fetchall()
    return [
        Candidate(
            microservice_name=r[0],
            http_method=r[1],
            endpoint_path=r[2],
            operation_id=r[3],
            semantic_description=r[4],
            raw_openapi_json=r[5],
            distance=float(r[6]),
        )
        for r in rows
    ]
