from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from common import compact_json, emit, fail, json_preview, load_json, project_root, read_input, rel, safe_run_dir, write_json, write_text

PERMISSION_ERROR_TERMS = ("permission", "denied", "not allowed", "forbidden", "not permitted", "access denied")

MAX_EVIDENCE_PER_ITEM = 20
MAX_DELEGATION_EDGES = 50
MAX_RECOVERY_ITEMS = 20

TASK_ID_RE = re.compile(r'<task id="(ses_[A-Za-z0-9_-]+)"')
FAILED_STATUSES = {"error", "failed", "failure"}
SUCCESS_STATUSES = {"completed", "success", "ok"}

SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "api-key", "access_key", "accesskey", "secret_key", "secretkey",
    "private_key", "privatekey", "authorization", "auth", "auth_token",
    "session_token", "sessiontoken", "credential", "credentials",
    "cookie", "set-cookie", "bearer", "client_secret", "clientsecret",
    "refresh_token", "refreshtoken", "id_token", "idtoken", "x-api-key",
    "x-auth-token", "ssh_key", "sshkey", "aws_secret_access_key",
    "aws_access_key_id", "google_api_key", "github_token", "npm_token",
    "slack_token", "webhook_url", "webhook", "signature", "signing_key",
}

CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"), 1),
    (re.compile(r"(?i)(AKIA[0-9A-Z]{16})"), 1),
    (re.compile(r"(?i)(ghp_[A-Za-z0-9]{20,})"), 1),
    (re.compile(r"(?i)(xox[baprs]-[A-Za-z0-9-]{10,})"), 1),
    (re.compile(r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----)"), 1),
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{10,})"), 2),
    (re.compile(r"(?i)(password\s*[=:]\s*)([^\s,;]+)"), 2),
    (re.compile(r"(?i)(token\s*[=:]\s*)([^\s,;]+)"), 2),
    (re.compile(r"(?i)(secret\s*[=:]\s*)([^\s,;]+)"), 2),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;]+)"), 2),
    (re.compile(r"(?i)(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"), 1),
]


def status_of(event: dict[str, Any]) -> str:
    return str(event.get("status") or "unknown")


def is_failed_tool(event: dict[str, Any]) -> bool:
    return event.get("type") == "tool" and status_of(event) in FAILED_STATUSES


def error_text(event: dict[str, Any]) -> str:
    return " ".join(str(x) for x in [event.get("error"), event.get("output")] if x is not None).lower()


def redact_string(text: str) -> str:
    masked = text
    for pattern, group in CREDENTIAL_PATTERNS:
        masked = pattern.sub(
            lambda m: m.group(0)[: m.start(group) - m.start(0)]
            + "[REDACTED]"
            + m.group(0)[m.end(group) - m.start(0):],
            masked,
        )
    return masked


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


def redact_preview(value: Any, limit: int = 500) -> str:
    return json_preview(redact(value), limit)


def bound_evidence(ids: list[Any], limit: int = MAX_EVIDENCE_PER_ITEM) -> dict[str, Any]:
    ids = [i for i in ids if i is not None]
    if len(ids) <= limit:
        return {"event_ids": ids, "truncated": False, "truncated_count": 0}
    return {"event_ids": ids[:limit], "truncated": True, "truncated_count": len(ids) - limit}


def extract_child_session_id(event: dict[str, Any]) -> str | None:
    meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    sid = meta.get("sessionId")
    if sid:
        return str(sid)
    output = event.get("output")
    if isinstance(output, str):
        match = TASK_ID_RE.search(output)
        if match:
            return match.group(1)
    inp = event.get("input") if isinstance(event.get("input"), dict) else {}
    task_id = inp.get("task_id")
    if task_id:
        return str(task_id)
    return None


def load_inline_blocks(block_descriptors: list[dict[str, Any]], run_dir: Any) -> tuple[list[dict[str, Any]], int]:
    blocks_out: list[dict[str, Any]] = []
    summarized_count = 0
    missing: list[str] = []
    for block in block_descriptors:
        chunk_id = block.get("chunk_id")
        if block.get("needs_summary"):
            summary_path = run_dir / "summaries" / f"{chunk_id}.json"
            if not summary_path.exists():
                missing.append(chunk_id)
                continue
            summary_payload = load_json(summary_path)
            semantic = summary_payload.get("summary", {})
            summary_text = redact_string(semantic.get("summary") or "")
            summarized_count += 1
            representation = {"mode": "summary", "semantic": redact(semantic)}
        else:
            content = block.get("content") or ""
            summary_text = redact_string(content)
            representation = {"mode": "verbatim", "content": redact_string(content)}
        blocks_out.append(
            {
                "chunk_id": chunk_id,
                "kind": block.get("kind"),
                "message_id": block.get("message_id"),
                "char_count": block.get("char_count"),
                "source_refs": block.get("source_refs", []),
                "metadata": redact(block.get("metadata", {})),
                "summary_text": summary_text,
                "representation": representation,
            }
        )
    if missing:
        raise ValueError(f"missing required summaries: {', '.join(missing)}")
    return blocks_out, summarized_count


def build_timeline(events: list[dict[str, Any]], blocks_out: list[dict[str, Any]], timeline_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_by_id = {e.get("event_id"): e for e in events}
    block_by_id = {b["chunk_id"]: b for b in blocks_out}
    timeline_out: list[dict[str, Any]] = []
    for item in timeline_refs:
        if item.get("type") == "block" and item.get("ref") in block_by_id:
            b = block_by_id[item["ref"]]
            timeline_out.append(
                {
                    "type": "block",
                    "ref": b["chunk_id"],
                    "kind": b["kind"],
                    "message_id": b["message_id"],
                    "summary_text": b["summary_text"],
                    "semantic": b["representation"].get("semantic"),
                }
            )
        elif item.get("type") == "event" and item.get("ref") in event_by_id:
            e = event_by_id[item["ref"]]
            detail = ""
            if e.get("type") == "tool":
                detail = f"input={redact_preview(e.get('input'), 500)}"
                if e.get("error"):
                    detail += f" error={redact_preview(e.get('error'), 500)}"
                elif e.get("output") is not None:
                    detail += f" output={redact_preview(e.get('output'), 500)}"
            else:
                detail = redact_preview({k: v for k, v in e.items() if k not in {"event_id", "type"}}, 700)
            timeline_out.append(
                {
                    "type": "event",
                    "ref": e.get("event_id"),
                    "event_type": e.get("type"),
                    "message_id": e.get("message_id"),
                    "tool": e.get("tool"),
                    "status": e.get("status"),
                    "detail": detail,
                }
            )
    return timeline_out


def analyze_session(
    view: dict[str, Any],
    *,
    is_root: bool,
    run_dir: Any,
) -> dict[str, Any]:
    blocks_out, summarized_count = load_inline_blocks(view.get("blocks", []), run_dir)

    events = view.get("events", [])
    tool_events = [e for e in events if e.get("type") == "tool"]
    failed = [e for e in tool_events if is_failed_tool(e)]
    permission_like = [e for e in failed if any(term in error_text(e) for term in PERMISSION_ERROR_TERMS)]

    failed_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in failed:
        key = f"{event.get('tool')}|{compact_json(event.get('input'))}"
        failed_groups[key].append(event)
    repeated = []
    for key, group in failed_groups.items():
        if len(group) < 2:
            continue
        repeated.append(
            {
                "tool": group[0].get("tool"),
                "input": group[0].get("input"),
                "count": len(group),
                "event_ids": [g.get("event_id") for g in group],
            }
        )

    status_counts = Counter(status_of(e) for e in tool_events)
    tool_counts = Counter(str(e.get("tool") or "unknown") for e in tool_events)
    by_tool_status: dict[str, Counter] = defaultdict(Counter)
    tool_evidence: dict[str, list[Any]] = defaultdict(list)
    for e in tool_events:
        t = str(e.get("tool") or "unknown")
        by_tool_status[t][status_of(e)] += 1
        tool_evidence[t].append(e.get("event_id"))

    timeline_out = build_timeline(events, blocks_out, view.get("timeline", []))

    task_events = [e for e in tool_events if str(e.get("tool") or "").lower() == "task"]
    child_ids = [extract_child_session_id(e) for e in task_events]
    child_ids = [c for c in child_ids if c]

    observed_agents = sorted(
        {str(m.get("agent")) for m in view.get("messages", []) if m.get("agent")}
    )

    return {
        "session_id": view.get("session_id"),
        "is_root": is_root,
        "session_info": view.get("session_info"),
        "totals": view.get("totals", {}),
        "observed_agents": observed_agents,
        "counts": {
            "messages": len(view.get("messages", [])),
            "blocks": len(blocks_out),
            "events": len(events),
            "tool_calls": len(tool_events),
            "failed_tool_calls": len(failed),
            "summarized_blocks": summarized_count,
        },
        "tool_statistics": {
            "by_tool": dict(tool_counts),
            "by_status": dict(status_counts),
            "by_tool_status": {t: dict(c) for t, c in by_tool_status.items()},
            "evidence": {
                "by_tool": {t: bound_evidence(ids) for t, ids in tool_evidence.items()},
            },
        },
        "deterministic_findings": {
            "permission_like_failures": [
                {
                    "event_id": e.get("event_id"),
                    "message_id": e.get("message_id"),
                    "tool": e.get("tool"),
                    "input": redact_preview(e.get("input"), 500),
                    "error": redact_preview(e.get("error"), 500),
                }
                for e in permission_like
            ],
            "repeated_failed_actions": [
                {
                    "tool": item.get("tool"),
                    "input": redact_preview(item.get("input"), 500),
                    "count": item.get("count"),
                    "event_ids": item.get("event_ids", []),
                }
                for item in repeated
            ],
            "retry_events": sum(1 for e in events if e.get("type") == "retry"),
            "subtask_events": sum(1 for e in events if e.get("type") == "subtask")
            + len(task_events),
        },
        "delegation": {
            "task_calls": len(task_events),
            "child_session_ids": child_ids,
        },
        "blocks": blocks_out,
        "timeline": timeline_out,
    }


def normalize_sessions(normalized: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    sessions = normalized.get("sessions")
    if isinstance(sessions, list) and sessions:
        views = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            info = s.get("session_info") if isinstance(s.get("session_info"), dict) else {}
            views.append(
                {
                    "session_id": s.get("session_id") or info.get("id") or "unknown",
                    "session_info": s.get("session_info"),
                    "events": s.get("events", []),
                    "timeline": s.get("timeline", []),
                    "totals": s.get("totals", {}),
                    "messages": s.get("messages", []),
                    "blocks": s.get("blocks", []),
                }
            )
        return views, "sessions_list"

    events = normalized.get("events", [])
    has_sid = any(isinstance(e, dict) and e.get("session_id") for e in events)
    if has_sid:
        by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in events:
            if isinstance(e, dict):
                by_sid[e.get("session_id") or "unknown"].append(e)
        views = []
        for sid, evs in by_sid.items():
            views.append(
                {
                    "session_id": sid,
                    "session_info": normalized.get("session_info"),
                    "events": evs,
                    "timeline": [t for t in normalized.get("timeline", []) if t.get("session_id") == sid],
                    "totals": normalized.get("totals", {}),
                    "messages": [m for m in normalized.get("messages", []) if m.get("session_id") == sid],
                    "blocks": [b for b in normalized.get("blocks", []) if b.get("session_id") == sid],
                }
            )
        return views, "combined"

    info = normalized.get("session_info") if isinstance(normalized.get("session_info"), dict) else {}
    return (
        [
            {
                "session_id": info.get("id") or "root",
                "session_info": normalized.get("session_info"),
                "events": events,
                "timeline": normalized.get("timeline", []),
                "totals": normalized.get("totals", {}),
                "messages": normalized.get("messages", []),
                "blocks": normalized.get("blocks", []),
            }
        ],
        "single",
    )


def build_delegation(views: list[dict[str, Any]]) -> dict[str, Any]:
    in_scope_ids = {v["session_id"] for v in views}
    edges: list[dict[str, Any]] = []
    task_calls = 0
    for v in views:
        for e in v.get("events", []):
            if e.get("type") != "tool" or str(e.get("tool") or "").lower() != "task":
                continue
            task_calls += 1
            child_sid = extract_child_session_id(e)
            in_scope = child_sid in in_scope_ids
            inp = e.get("input") if isinstance(e.get("input"), dict) else {}
            edges.append(
                {
                    "parent_session_id": v["session_id"],
                    "child_session_id": child_sid,
                    "in_scope": in_scope,
                    "task_event_id": e.get("event_id"),
                    "message_id": e.get("message_id"),
                    "call_id": e.get("call_id"),
                    "subagent_type": inp.get("subagent_type"),
                    "background": inp.get("background"),
                    "status": status_of(e),
                }
            )
    in_scope_children = {e["child_session_id"] for e in edges if e["in_scope"]}
    out_of_scope = [e for e in edges if not e["in_scope"]]
    return {
        "task_calls": task_calls,
        "children_in_scope": len(in_scope_children),
        "children_out_of_scope": len(out_of_scope),
        "edges": edges[:MAX_DELEGATION_EDGES],
        "edges_truncated": len(edges) > MAX_DELEGATION_EDGES,
        "edges_truncated_count": max(0, len(edges) - MAX_DELEGATION_EDGES),
    }


def build_recoveries(views: list[dict[str, Any]]) -> dict[str, Any]:
    tool_recoveries: list[dict[str, Any]] = []
    delegation_recoveries: list[dict[str, Any]] = []
    for v in views:
        last_failed: dict[str, dict[str, Any]] = {}
        for e in v.get("events", []):
            if e.get("type") != "tool":
                continue
            t = str(e.get("tool") or "unknown")
            if t.lower() == "task":
                continue
            st = status_of(e)
            if st in FAILED_STATUSES:
                last_failed[t] = e
            elif st in SUCCESS_STATUSES and t in last_failed:
                tool_recoveries.append(
                    {
                        "session_id": v["session_id"],
                        "kind": "tool",
                        "tool": t,
                        "failed_event_id": last_failed[t].get("event_id"),
                        "recovered_event_id": e.get("event_id"),
                        "message_id": e.get("message_id"),
                    }
                )
                del last_failed[t]

    delegation_fallbacks: list[dict[str, Any]] = []
    for v in views:
        pending_task_fail: dict[str, Any] | None = None
        for e in v.get("events", []):
            if e.get("type") != "tool" or str(e.get("tool") or "").lower() != "task":
                continue
            st = status_of(e)
            if st in FAILED_STATUSES:
                pending_task_fail = e
            elif st in SUCCESS_STATUSES and pending_task_fail is not None:
                failed_inp = pending_task_fail.get("input") if isinstance(pending_task_fail.get("input"), dict) else {}
                rec_inp = e.get("input") if isinstance(e.get("input"), dict) else {}
                failed_sub = failed_inp.get("subagent_type")
                rec_sub = rec_inp.get("subagent_type")
                failed_child = extract_child_session_id(pending_task_fail)
                rec_child = extract_child_session_id(e)
                same_target = (failed_child is not None and failed_child == rec_child) or (
                    failed_child is None and rec_child is None and failed_sub == rec_sub
                )
                item = {
                    "session_id": v["session_id"],
                    "kind": "delegation",
                    "failed_event_id": pending_task_fail.get("event_id"),
                    "recovered_event_id": e.get("event_id"),
                    "message_id": e.get("message_id"),
                    "failed_subagent_type": failed_sub,
                    "recovered_subagent_type": rec_sub,
                    "failed_child_session_id": failed_child,
                    "recovered_child_session_id": rec_child,
                }
                if same_target:
                    item["classification"] = "retry"
                    delegation_recoveries.append(item)
                else:
                    item["classification"] = "fallback"
                    delegation_fallbacks.append(item)
                pending_task_fail = None

    return {
        "tool_recoveries": tool_recoveries[:MAX_RECOVERY_ITEMS],
        "tool_recoveries_truncated": len(tool_recoveries) > MAX_RECOVERY_ITEMS,
        "tool_recoveries_truncated_count": max(0, len(tool_recoveries) - MAX_RECOVERY_ITEMS),
        "delegation_recoveries": delegation_recoveries[:MAX_RECOVERY_ITEMS],
        "delegation_recoveries_truncated": len(delegation_recoveries) > MAX_RECOVERY_ITEMS,
        "delegation_recoveries_truncated_count": max(0, len(delegation_recoveries) - MAX_RECOVERY_ITEMS),
        "delegation_fallbacks": delegation_fallbacks[:MAX_RECOVERY_ITEMS],
        "delegation_fallbacks_truncated": len(delegation_fallbacks) > MAX_RECOVERY_ITEMS,
        "delegation_fallbacks_truncated_count": max(0, len(delegation_fallbacks) - MAX_RECOVERY_ITEMS),
        "total": len(tool_recoveries) + len(delegation_recoveries) + len(delegation_fallbacks),
    }


def render_markdown(session_map: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Session evaluation map — {session_map['run_id']}")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    counts = session_map["counts"]
    lines.append(f"- Messages: {counts['messages']}")
    lines.append(f"- Semantic blocks: {counts['blocks']}")
    lines.append(f"- Tool/events: {counts['events']}")
    lines.append(f"- Tool calls: {counts['tool_calls']}")
    lines.append(f"- Failed tool calls: {counts['failed_tool_calls']}")
    lines.append(f"- SLM summaries: {counts['summarized_blocks']}")
    multi = session_map.get("multi_session", {})
    if multi.get("session_count", 1) > 1:
        lines.append(f"- Sessions: {multi['session_count']} (mode: {multi.get('mode', '?')})")
    lines.append("")
    lines.append("## Deterministic findings")
    lines.append("")
    findings = session_map["deterministic_findings"]
    lines.append(f"- Permission-like tool failures: {len(findings['permission_like_failures'])}")
    lines.append(f"- Repeated failed tool/input groups: {len(findings['repeated_failed_actions'])}")
    lines.append(f"- Retry events: {findings['retry_events']}")
    lines.append(f"- Subtask events: {findings['subtask_events']}")
    lines.append("")
    delegation = session_map.get("delegation", {})
    if delegation:
        lines.append("## Delegation")
        lines.append("")
        lines.append(f"- Task calls: {delegation.get('task_calls', 0)}")
        lines.append(f"- Children in scope: {delegation.get('children_in_scope', 0)}")
        lines.append(f"- Children out of scope: {delegation.get('children_out_of_scope', 0)}")
        lines.append("")
    recoveries = session_map.get("recoveries", {})
    if recoveries:
        lines.append("## Recoveries")
        lines.append("")
        lines.append(f"- Tool recoveries: {len(recoveries.get('tool_recoveries', []))}")
        lines.append(f"- Delegation recoveries: {len(recoveries.get('delegation_recoveries', []))}")
        lines.append("")
    lines.append("## Timeline")
    lines.append("")
    for item in session_map["timeline"]:
        if item["type"] == "block":
            lines.append(f"### {item['ref']} · {item['kind']} · message {item['message_id']}")
            lines.append("")
            lines.append(item.get("summary_text") or "(empty)")
            lines.append("")
        else:
            label = item.get("tool") or item.get("event_type") or "event"
            status = f" · {item['status']}" if item.get("status") else ""
            lines.append(f"- `{item['ref']}` {label}{status}: {item.get('detail', '')}")
    lines.append("")
    lines.append("## Prompt copies")
    lines.append("")
    for prompt in session_map.get("prompts", []):
        lines.append(f"- `{prompt['role']}` → `{prompt['run_path']}` ({prompt['sha256'][:12]}…)")
    if not session_map.get("prompts"):
        lines.append("- No prompts were supplied to this run.")
    return "\n".join(lines) + "\n"


def main() -> None:
    try:
        data = read_input()
        root = project_root(data)
        run_dir = safe_run_dir(root, str(data.get("run_id", "")))
        manifest = load_json(run_dir / "manifest.json")
        normalized = load_json(run_dir / "normalized" / "session.json")

        views, mode = normalize_sessions(normalized)
        existing_ids = {v["session_id"] for v in views}

        for child in manifest.get("session_tree", {}).get("child_session_copies", []):
            sid = child.get("session_id")
            if sid not in existing_ids:
                raise ValueError(
                    f"child session {sid} copied by prepare but missing from normalized.sessions; "
                    f"cannot build a reliable map for it"
                )

        root_info = normalized.get("session_info") if isinstance(normalized.get("session_info"), dict) else {}
        root_sid = root_info.get("id")
        root_index = 0
        if root_sid:
            for i, v in enumerate(views):
                if v["session_id"] == root_sid:
                    root_index = i
                    break
        root_view = views[root_index]

        root_session = analyze_session(root_view, is_root=True, run_dir=run_dir)

        sessions_out: list[dict[str, Any]] = []
        for i, v in enumerate(views):
            if i == root_index:
                sessions_out.append(root_session)
            else:
                sessions_out.append(analyze_session(v, is_root=False, run_dir=run_dir))

        delegation = build_delegation(views)
        recoveries = build_recoveries(views)

        session_tree = manifest.get("session_tree", {})
        session_map = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "source": manifest["source"],
            "prompts": manifest.get("prompts", []),
            "session_info": root_session["session_info"],
            "totals": root_session["totals"],
            "counts": root_session["counts"],
            "tool_statistics": root_session["tool_statistics"],
            "deterministic_findings": root_session["deterministic_findings"],
            "blocks": root_session["blocks"],
            "timeline": root_session["timeline"],
            "multi_session": {
                "mode": mode,
                "session_count": len(views),
                "root_session_id": root_session["session_id"],
            },
            "sessions": sessions_out,
            "delegation": delegation,
            "recoveries": recoveries,
            "session_tree": {
                "missing_child_exports": session_tree.get("missing_child_exports", []),
                "ambiguous_child_exports": session_tree.get("ambiguous_child_exports", []),
                "missing_agent_prompts": session_tree.get("missing_agent_prompts", []),
                "ambiguous_agent_prompts": session_tree.get("ambiguous_agent_prompts", []),
                "edges": session_tree.get("edges", []),
                "unresolved_tasks": session_tree.get("unresolved_tasks", []),
            },
        }

        json_path = run_dir / "derived" / "session-map.json"
        md_path = run_dir / "derived" / "session-map.md"
        write_json(json_path, session_map)
        write_text(md_path, render_markdown(session_map))
        emit(
            {
                "ok": True,
                "run_id": manifest["run_id"],
                "session_map_path": rel(root, json_path),
                "session_map_markdown_path": rel(root, md_path),
                "prompt_paths": [p["run_path"] for p in manifest.get("prompts", [])],
                "deterministic_findings": session_map["deterministic_findings"],
                "multi_session": session_map["multi_session"],
                "delegation": {
                    "task_calls": delegation["task_calls"],
                    "children_in_scope": delegation["children_in_scope"],
                    "children_out_of_scope": delegation["children_out_of_scope"],
                },
                "recoveries": {
                    "tool_recoveries": len(recoveries["tool_recoveries"]),
                    "delegation_recoveries": len(recoveries["delegation_recoveries"]),
                },
                "session_tree": session_map["session_tree"],
            }
        )
    except Exception as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
