# RFC 0001 — Server-side `searchLogs` + correlation-id filtering for the AWC diagnostics service

- **Status:** Draft
- **Author:** plore team
- **Audience:** AWC Core / diagnostics service owners (`awc-core/services/diagnostics`)
- **Related:** plore agentic diagnose→retry loop; JIRA `AWC` / component "AWC Core"

## Summary

Add a `POST /api/v1/diagnostics/searchLogs` endpoint that filters, redacts, and caps log lines
**server-side** and returns only the matching lines as JSON. Search must work by **any string** —
arbitrary free-text/substring (and optionally regex) matching against the log message — as a
first-class filter, usable on its own (bounded by namespace/time) without any pod name. A
**correlation id** (W3C trace-id / `X-Request-Id`) is one specialized, indexed case of this: it
retrieves exactly the lines belonging to one logical request across pods. Both the general string
search and the correlation-id search are core to this endpoint; neither is privileged over the
other.

## Motivation

plore's troubleshooting agent diagnoses a failed AWC operation by reading logs. Today the only
read paths are:

1. `GET /api/v1/diagnostics/downloadFile?pod_name=&namespace=&tail_lines=` — live pod logs as JSON,
   but it requires knowing the **exact failed pod name**. When diagnosing an async deploy that
   fails minutes later in a provisioning Job, the agent does not know which pod failed.
2. `collect → status → download` — the `logs.tar.lz4` bundle. This forces the agent to download a
   multi-megabyte tar into its context (or session storage) and grep it locally. It has OTLP
   ingestion lag, no redaction guarantees at the line level, and is wasteful for "find the error
   for *this* request" queries.

Neither supports the natural diagnostic question: *"show me the error lines for the request I just
made."* The agent currently stamps every outbound AWC call with `X-Request-Id` + W3C `traceparent`
(one correlation id per turn, reused across retries and probes). If the diagnostics service can
filter by that id, the agent gets precise, bounded, redacted evidence in one call.

**Predicate pushdown:** filtering belongs at the service that owns the logs, not in the agent.
The service can index by namespace/label/time, enforce tenancy, apply redaction rules per line,
and cap result size — none of which the client can do safely or cheaply.

## Proposal

### Endpoint

```
POST /api/v1/diagnostics/searchLogs
Authorization: Bearer <Knox/JWT>            # same auth as existing diagnostics endpoints
Content-Type: application/json
```

Request body (all filters optional; AND-combined; at least one of `pattern`, `correlationId`,
`podName`, `labelSelector`, or `namespaceList` required to bound the scan). Any single filter is
sufficient — e.g. a bare `pattern` (with a default recent `timeRange` + entitled namespaces) is a
valid full-text search; `correlationId` alone is valid; combinations narrow further:

```jsonc
{
  "namespaceList": ["awc-core"],            // restricted to caller entitlement
  "podName": "console-7c9...",              // optional exact pod
  "labelSelector": "app=console",           // k8s label selector
  "timeRange": { "start": "<rfc3339>", "end": "<rfc3339>" },
  "logLevel": "error",                      // minimum level
  "pattern": "application not found",       // ANY string — free-text substring (or regex); see ReDoS guard
  "correlationId": "9f8e...32hex",          // X-Request-Id / W3C trace-id (an indexed special case)
  "contextLines": 3,                        // grep -C: N lines before AND after each match (per pod)
  "contextBefore": 3,                       // grep -B: overrides contextLines for the leading side
  "contextAfter": 3,                        // grep -A: overrides contextLines for the trailing side
  "limit": 200                              // hard-capped server-side (e.g. max 1000) — counts MATCHED lines
}
```

Response (`200`). When context is requested, lines are returned as **blocks** — each block is a
contiguous, time-ordered run of lines from one pod/container, with matched lines flagged. Without
context (`contextLines`/`Before`/`After` all 0 or absent), each match is simply its own
single-line block.

```jsonc
{
  "matched": 17,                            // number of MATCHED lines (not counting context)
  "truncated": false,                       // true if limit/time/byte cap hit
  "blocks": [
    {
      "namespace": "awc-core", "pod": "console-7c9...", "container": "console",
      "lines": [
        { "ts": "<rfc3339>", "level": "info",  "correlationId": "9f8e...", "matched": false, "message": "<redacted>" },
        { "ts": "<rfc3339>", "level": "error", "correlationId": "9f8e...", "matched": true,  "message": "application not found" },
        { "ts": "<rfc3339>", "level": "info",  "correlationId": "9f8e...", "matched": false, "message": "<redacted>" }
      ]
    }
  ]
}
```

### Behavior & guarantees

- **Search by any string.** `pattern` matches arbitrary free text against the log message —
  substring by default, regex optionally (see ReDoS guard). It is a primary, standalone filter: a
  caller can search for any string (e.g. `"application not found"`, an app name, an error code)
  across the entitled namespaces and a time window without supplying a correlation id or pod name.
- **Correlation id is the indexed special case.** `correlationId` retrieves one request's lines
  across all pods that handled it. It is functionally a fast, exact-match path over the same log
  store as `pattern` — important for the agent's "this request I just made" case, but not a
  precondition for searching. Requires services to log the incoming `X-Request-Id` / propagate
  `traceparent` (most already receive it from the gateway).
- **Surrounding context, grep-style.** `contextLines` (and the asymmetric `contextBefore` /
  `contextAfter`) return N lines of leading/trailing context around each match — the equivalent of
  `grep -C` / `-B` / `-A`. A single error line is rarely self-explanatory; the stack frame or the
  request line just above it usually is. Context is taken from the same pod/container/log stream as
  the match, in original order.
- **No duplicated or overlapping context.** When two matches are close enough that their context
  windows overlap or abut (including adjacent matched lines), they are **coalesced into one block**
  rather than emitted as separate, overlapping windows — exactly as `grep` merges into a single run
  with no repeated lines. Every source line appears **at most once** in the response, and matched
  lines within a merged block keep their `matched: true` flag. This keeps the payload minimal and
  avoids the agent re-reading the same lines. `matched` counts only the matching lines, so `limit`
  bounds matches while context lines ride along.
- **Redaction at the line level.** Reuse the existing diagnostics redaction-rules engine on every
  returned `message`. Bearer tokens, `Authorization` headers, and secrets must never appear in
  output — this is stricter than the raw tar today.
- **Tenancy.** `namespaceList` is intersected with the caller's entitlement (same model as
  `collect`); a request for a namespace the caller cannot see returns empty, not an error leak.
- **Bounded cost.** `limit` is hard-capped server-side; enforce a byte cap and a scan-time budget.
  Return `truncated: true` rather than scanning unbounded history. Default `timeRange` to a recent
  window (e.g. last 1h) when omitted.
- **ReDoS / injection guard.** Treat `pattern` as a literal substring by default; if regex is
  supported, compile with a size/complexity limit and a per-line match timeout (RE2-style, no
  backtracking). Reject patterns over N chars.
- **Source freshness.** Prefer the live k8s log API (like `downloadFile`) for recency; fall back to
  ingested store for `timeRange` windows older than the live retention.

## Why not the existing endpoints

| Need | `downloadFile` | `collect`+`download` (tar) | `searchLogs` |
|---|---|---|---|
| No pod name known | ✗ requires pod | ~ (broad, then grep) | ✓ filter by label/correlationId |
| Cross-pod for one request | ✗ | ~ local grep over tar | ✓ correlationId |
| Bounded/redacted result | ~ tail only | ✗ raw tar, client redacts | ✓ server caps + redacts |
| No large download into agent | ✓ | ✗ MBs into context/S3 | ✓ only matching lines |
| Recency (no ingestion lag) | ✓ live | ✗ OTLP lag | ✓ live-first |

## plore-side adoption (already shipped, degrades gracefully)

`plore/awc_api.py::search_logs(...)` calls this endpoint first. Until it exists, it falls back to a
**bounded** `downloadFile` + local grep when a pod is known, and otherwise returns
`source: "unavailable"` rather than downloading a tar. No agent change is needed when the endpoint
ships — `search_logs` will simply start returning `source: "server"`.

## Out of scope

- Streaming/tailing (`follow`) — a later enhancement.
- Aggregations/metrics over logs.
- Cross-cluster federation.

## Rollout

1. Implement `searchLogs` with `pattern` (any-string search) + `namespaceList` + `timeRange`
   filters and grep-style `contextLines` (with overlap-merged blocks) behind the existing auth —
   the general full-text case, usable on its own.
2. Add `correlationId` as an indexed exact-match filter; confirm the gateway propagates
   `X-Request-Id` / `traceparent` to backend services and add structured logging of the id where
   missing.
3. Add `labelSelector` / `logLevel` to narrow further.
4. plore flips to `source: "server"` automatically.
