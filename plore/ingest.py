"""Ingest OpenAPI specs into the pgvector registry.

Sources (in priority order):
  - SPECS_DIR: a directory holding <service>/openapi.yaml (e.g. awc-core/api).
  - a single bundled JSON file (the awc-mcp `get_api_specs` shape) via --bundle.

Run:  SPECS_DIR=/path/to/awc-core/api plore-ingest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import db, llm
from .config import config
from .semantic import Operation, iter_operations, semantic_description

_EMBED_BATCH = 64

# Deterministic intent overlay for operations whose AWC terminology diverges from how users
# phrase requests (semantic search + a small embedder won't bridge these reliably on its own).
_INTENT_OVERRIDES: dict[str, str] = {
    "deployApp": "create a cluster, provision a cluster, launch a cluster, spin up a cluster, "
    "create a new cluster, create an experience, deploy a workload, deploy an application",
}

# Short domain hint so the model maps AWC terminology when generating intents.
_AWC_HINT = (
    "AWC domain: a cluster, experience, or deployment is CREATED/PROVISIONED by deploying an "
    "application (the deployApp operation). Engine/EngineInstance are service templates/instances."
)


def _enrich_intent(method: str, path: str, base_desc: str) -> str:
    """A comma-separated list of user-intent synonyms for an operation, to improve recall."""
    try:
        out = llm.chat(
            [
                {
                    "role": "system",
                    "content": "Output ONLY a comma-separated list of short natural-language "
                    "user-request phrases (include synonyms like create/provision/launch/list/"
                    "delete) that should map to this AWC API operation, to improve semantic "
                    "search. No preamble, no numbering, no quotes. " + _AWC_HINT,
                },
                {"role": "user", "content": f"{method} {path} — {base_desc}\nPhrases:"},
            ],
            max_tokens=120,
        )
    except Exception:  # noqa: BLE001 - enrichment is best-effort; fall back to base description
        return ""
    if not out or not out.strip():
        return ""
    txt = " ".join(out.split())  # collapse newlines/whitespace so multi-line lists survive
    low = txt.lower()
    for lead in ("here", "the following", "sure", "below", "these are"):
        if low.startswith(lead) and ":" in txt:  # drop a leading "Here is ...:" preamble
            txt = txt.split(":", 1)[1].strip()
            break
    return txt[:300]


def _load_specs_from_dir(specs_dir: Path) -> dict[str, dict]:
    """Map service-name -> spec dict from <service>/openapi.{yaml,json} files."""
    specs: dict[str, dict] = {}
    for path in sorted(specs_dir.glob("*/openapi.y*ml")) + sorted(specs_dir.glob("*/openapi.json")):
        service = path.parent.name
        specs[service] = yaml.safe_load(path.read_text())
    return specs


def _parse_bundle(data: dict) -> dict[str, dict]:
    """Parse the awc-mcp get_api_specs bundle: {service: {content: "<yaml>", ...}}."""
    specs: dict[str, dict] = {}
    for service, entry in data.items():
        content = entry.get("content") if isinstance(entry, dict) else None
        if content:
            specs[service] = yaml.safe_load(content)
    return specs


def _load_specs_from_bundle(bundle_path: Path) -> dict[str, dict]:
    return _parse_bundle(json.loads(bundle_path.read_text()))


def _load_specs_from_mcp(url: str, token: str = "") -> dict[str, dict]:
    """Fetch specs from awc-mcp's `get_api_specs` tool over streamable HTTP MCP.

    Cluster-native source: the OpenAPI files are not present on the cluster, but
    awc-mcp compiles them in and serves them. Needs a JWT if awc-mcp enforces auth.
    """
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else None

    async def _run() -> dict:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_api_specs", {})
        text = next((c.text for c in result.content if getattr(c, "type", None) == "text"), None)
        if not text:
            raise RuntimeError("awc-mcp get_api_specs returned no text content")
        return json.loads(text)

    return _parse_bundle(asyncio.run(_run()))


def collect_operations(specs: dict[str, dict]) -> list[Operation]:
    ops: list[Operation] = []
    for service, spec in specs.items():
        ops.extend(iter_operations(spec, service))
    return ops


def ingest(specs: dict[str, dict]) -> int:
    ops = collect_operations(specs)
    if not ops:
        print("No operations found in specs.", file=sys.stderr)
        return 0

    descriptions = [semantic_description(op) for op in ops]
    if config.enrich_descriptions:
        print(f"Enriching {len(ops)} descriptions with intent synonyms (LLM)...", file=sys.stderr)
        for i, op in enumerate(ops):
            intent = _enrich_intent(op.http_method, op.endpoint_path, descriptions[i])
            if intent:
                descriptions[i] = f"{descriptions[i]} Intents: {intent}"
            if (i + 1) % 10 == 0:
                print(f"  enriched {i + 1}/{len(ops)}", file=sys.stderr)

    # Curated overlay: always applied, independent of LLM enrichment.
    for i, op in enumerate(ops):
        override = _INTENT_OVERRIDES.get(op.operation_id or "")
        if override:
            descriptions[i] = f"{descriptions[i]} Intents: {override}"

    conn = db.connect()
    db.ensure_schema(conn)

    # Per-service metadata from each spec's OpenAPI info block (grounds meta answers).
    for service, spec in specs.items():
        info = spec.get("info") or {}
        db.upsert_service(
            conn,
            project_id=config.project_id,
            microservice_name=service,
            title=(info.get("title") or "").strip() or None,
            description=(info.get("description") or "").strip() or None,
        )
    conn.commit()

    written = 0
    for start in range(0, len(ops), _EMBED_BATCH):
        batch_ops = ops[start : start + _EMBED_BATCH]
        batch_desc = descriptions[start : start + _EMBED_BATCH]
        vectors = llm.embed(batch_desc)
        for op, desc, vec in zip(batch_ops, batch_desc, vectors):
            db.upsert_operation(
                conn,
                project_id=config.project_id,
                microservice_name=op.microservice_name,
                http_method=op.http_method,
                endpoint_path=op.endpoint_path,
                operation_id=op.operation_id,
                raw_openapi_json=op.raw,
                body_schema=op.body_schema,
                semantic_description=desc,
                embedding=vec,
            )
            written += 1
        conn.commit()
        print(f"  embedded+upserted {written}/{len(ops)}", file=sys.stderr)

    conn.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest OpenAPI specs into pgvector.")
    parser.add_argument("--from-mcp", default=config.awc_mcp_url or None,
                        help="awc-mcp streamable-HTTP URL, e.g. http://awc-mcp:8080/mcp (cluster source)")
    parser.add_argument("--specs-dir", default=config.specs_dir or None, help="local <service>/openapi.yaml dir")
    parser.add_argument("--bundle", default=None, help="awc-mcp get_api_specs JSON bundle file")
    args = parser.parse_args()

    if args.from_mcp:
        specs = _load_specs_from_mcp(args.from_mcp, config.awc_mcp_token)
    elif args.bundle:
        specs = _load_specs_from_bundle(Path(args.bundle))
    elif args.specs_dir:
        specs = _load_specs_from_dir(Path(args.specs_dir))
    else:
        parser.error("provide --from-mcp/AWC_MCP_URL, --bundle, or --specs-dir/SPECS_DIR")

    print(f"Loaded {len(specs)} service spec(s): {', '.join(specs)}", file=sys.stderr)
    n = ingest(specs)
    print(f"Done. {n} operations in registry (project_id={config.project_id}).", file=sys.stderr)


if __name__ == "__main__":
    main()
