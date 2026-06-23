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
    body_schema: dict | None = None  # {required, properties, example} resolved from $ref


def _deref(node, spec, _depth=0):
    """Resolve a single $ref against the spec (one hop, bounded depth)."""
    if _depth > 5 or not isinstance(node, dict):
        return node if isinstance(node, dict) else {}
    if "$ref" in node:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = (target or {}).get(part, {}) if isinstance(target, dict) else {}
        return _deref(target, spec, _depth + 1)
    return node


def body_schema_from_op(op: dict, spec: dict) -> dict | None:
    """Resolve an operation's JSON requestBody into {required, properties, example}."""
    content = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
    if not content:
        return None
    schema = _deref(content.get("schema") or {}, spec)
    props = {}
    for name, pdef in (schema.get("properties") or {}).items():
        pdef = _deref(pdef, spec) if isinstance(pdef, dict) else {}
        props[name] = {
            "type": pdef.get("type"),
            "description": (pdef.get("description") or "")[:120],
        }
    return {
        "required": schema.get("required") or [],
        "properties": props,
        "example": content.get("example"),
    }


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
                body_schema=body_schema_from_op(op, spec),
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
