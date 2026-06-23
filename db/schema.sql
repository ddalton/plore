-- plore vector registry (MAUI guide §3). One row per OpenAPI operation.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS api_endpoint_registry (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          VARCHAR(100) NOT NULL,
    microservice_name   VARCHAR(100) NOT NULL,
    http_method         VARCHAR(10)  NOT NULL,
    endpoint_path       VARCHAR(255) NOT NULL,
    operation_id        VARCHAR(255),
    raw_openapi_json    JSONB        NOT NULL,
    semantic_description TEXT        NOT NULL,
    embedding           VECTOR(384)  NOT NULL,
    UNIQUE (project_id, microservice_name, http_method, endpoint_path)
);

-- HNSW for millisecond ANN over cosine distance (MAUI guide §2).
CREATE INDEX IF NOT EXISTS api_endpoint_registry_embedding_hnsw
    ON api_endpoint_registry USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS api_endpoint_registry_project
    ON api_endpoint_registry (project_id);
