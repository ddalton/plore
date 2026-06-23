"""pgvector registry access: schema bootstrap, idempotent upsert, top-k retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from .config import config

# Authoritative schema (kept in sync with db/schema.sql, which is the human-readable copy).
# Inlined so it works whether the package is installed editable or into site-packages.
_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS api_endpoint_registry (
    id                   BIGSERIAL PRIMARY KEY,
    project_id           VARCHAR(100) NOT NULL,
    microservice_name    VARCHAR(100) NOT NULL,
    http_method          VARCHAR(10)  NOT NULL,
    endpoint_path        VARCHAR(255) NOT NULL,
    operation_id         VARCHAR(255),
    raw_openapi_json     JSONB        NOT NULL,
    semantic_description TEXT         NOT NULL,
    embedding            VECTOR(384)  NOT NULL,
    UNIQUE (project_id, microservice_name, http_method, endpoint_path)
);

CREATE INDEX IF NOT EXISTS api_endpoint_registry_embedding_hnsw
    ON api_endpoint_registry USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS api_endpoint_registry_project
    ON api_endpoint_registry (project_id);

-- Per-service metadata (from each spec's OpenAPI info block) to ground meta answers.
CREATE TABLE IF NOT EXISTS service_catalog (
    project_id        VARCHAR(100) NOT NULL,
    microservice_name VARCHAR(100) NOT NULL,
    title             TEXT,
    description       TEXT,
    PRIMARY KEY (project_id, microservice_name)
);
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(config.database_url)
    # The `vector` type must exist before psycopg can register its adapter.
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA_SQL)
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


def upsert_service(
    conn: psycopg.Connection,
    *,
    project_id: str,
    microservice_name: str,
    title: str | None,
    description: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO service_catalog (project_id, microservice_name, title, description)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, microservice_name)
        DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description
        """,
        (project_id, microservice_name, title, description),
    )


def service_catalog(conn: psycopg.Connection, *, project_id: str) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT microservice_name, COALESCE(title, ''), COALESCE(description, '') "
        "FROM service_catalog WHERE project_id = %s ORDER BY microservice_name",
        (project_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


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
