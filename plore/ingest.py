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


def _load_specs_from_dir(specs_dir: Path) -> dict[str, dict]:
    """Map service-name -> spec dict from <service>/openapi.{yaml,json} files."""
    specs: dict[str, dict] = {}
    for path in sorted(specs_dir.glob("*/openapi.y*ml")) + sorted(specs_dir.glob("*/openapi.json")):
        service = path.parent.name
        specs[service] = yaml.safe_load(path.read_text())
    return specs


def _load_specs_from_bundle(bundle_path: Path) -> dict[str, dict]:
    """Parse the awc-mcp get_api_specs bundle: {service: {content: "<yaml>", ...}}."""
    data = json.loads(bundle_path.read_text())
    specs: dict[str, dict] = {}
    for service, entry in data.items():
        content = entry.get("content") if isinstance(entry, dict) else None
        if content:
            specs[service] = yaml.safe_load(content)
    return specs


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

    conn = db.connect()
    db.ensure_schema(conn)

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
    parser.add_argument("--specs-dir", default=config.specs_dir or None)
    parser.add_argument("--bundle", default=None, help="awc-mcp get_api_specs JSON bundle")
    args = parser.parse_args()

    if args.bundle:
        specs = _load_specs_from_bundle(Path(args.bundle))
    elif args.specs_dir:
        specs = _load_specs_from_dir(Path(args.specs_dir))
    else:
        parser.error("provide --specs-dir or SPECS_DIR, or --bundle")

    print(f"Loaded {len(specs)} service spec(s): {', '.join(specs)}", file=sys.stderr)
    n = ingest(specs)
    print(f"Done. {n} operations in registry (project_id={config.project_id}).", file=sys.stderr)


if __name__ == "__main__":
    main()
