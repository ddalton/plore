"""Turn OpenAPI specs into operation-level rows + natural-language descriptions.

Per MAUI guide §3, the embedded text is a descriptive summary of the endpoint's
capability, not raw schema code, e.g.:
  "Service: awc-console. Endpoint: POST /api/v0/console/clusters. Create a cluster.
   Provisions a new Kubernetes cluster. Parameters: body(clusterSpec)."
"""

from __future__ import annotations

from dataclasses import dataclass

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


@dataclass
class Operation:
    microservice_name: str
    http_method: str
    endpoint_path: str
    operation_id: str | None
    raw: dict


def iter_operations(spec: dict, microservice_name: str):
    """Yield one Operation per (path, method) in an OpenAPI document."""
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            yield Operation(
                microservice_name=microservice_name,
                http_method=method.upper(),
                endpoint_path=path,
                operation_id=op.get("operationId"),
                raw=op,
            )


def _param_hint(op: dict) -> str:
    names = [p.get("name") for p in op.get("parameters", []) if isinstance(p, dict) and p.get("name")]
    if op.get("requestBody"):
        names.append("body")
    return ", ".join(names)


def semantic_description(op: Operation) -> str:
    summary = (op.raw.get("summary") or "").strip()
    description = (op.raw.get("description") or "").strip()
    tags = ", ".join(op.raw.get("tags", []))
    params = _param_hint(op.raw)

    parts = [f"Service: {op.microservice_name}.",
             f"Endpoint: {op.http_method} {op.endpoint_path}."]
    if summary:
        parts.append(summary if summary.endswith(".") else summary + ".")
    if description and description != summary:
        # Keep it tight — first sentence is plenty for retrieval.
        first = description.split(". ")[0].strip().rstrip(".")
        parts.append(first + ".")
    if tags:
        parts.append(f"Tags: {tags}.")
    if params:
        parts.append(f"Parameters: {params}.")
    return " ".join(parts)
