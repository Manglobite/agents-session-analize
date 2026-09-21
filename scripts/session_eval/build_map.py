from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from common import compact_json, emit, fail, json_preview, load_json, project_root, read_input, rel, safe_run_dir, write_json, write_text

PERMISSION_ERROR_TERMS = ("permission", "denied", "not allowed", "forbidden", "not permitted", "access denied")


def status_of(event: dict[str, Any]) -> str:
    return str(event.get("status") or "unknown")


def is_failed_tool(event: dict[str, Any]) -> bool:
    return event.get("type") == "tool" and status_of(event) in {"error", "failed", "failure"}


def error_text(event: dict[str, Any]) -> str:
    return " ".join(str(x) for x in [event.get("error"), event.get("output")] if x is not None).lower()


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
    lines.append("")
    lines.append("## Deterministic findings")
    lines.append("")
    findings = session_map["deterministic_findings"]
    lines.append(f"- Permission-like tool failures: {len(findings['permission_like_failures'])}")
    lines.append(f"- Repeated failed tool/input groups: {len(findings['repeated_failed_actions'])}")
    lines.append(f"- Retry events: {findings['retry_events']}")
    lines.append(f"- Subtask events: {findings['subtask_events']}")
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

        blocks_out: list[dict[str, Any]] = []
        missing: list[str] = []
        summarized_count = 0
        for block in manifest.get("blocks", []):
            chunk = load_json(root / block["chunk_path"])
            if block.get("needs_summary"):
                summary_path = run_dir / "summaries" / f"{block['chunk_id']}.json"
                if not summary_path.exists():
                    missing.append(block["chunk_id"])
                    continue
                summary_payload = load_json(summary_path)
                semantic = summary_payload.get("summary", {})
                summary_text = semantic.get("summary") or ""
                summarized_count += 1
                representation = {"mode": "summary", "semantic": semantic}
            else:
                content = chunk.get("content") or ""
                summary_text = content
                representation = {"mode": "verbatim", "content": content}
            blocks_out.append(
                {
                    "chunk_id": block["chunk_id"],
                    "kind": block["kind"],
                    "message_id": block["message_id"],
                    "char_count": block["char_count"],
                    "source_refs": block.get("source_refs", []),
                    "metadata": block.get("metadata", {}),
                    "summary_text": summary_text,
                    "representation": representation,
                }
            )
        if missing:
            raise ValueError(f"missing required summaries: {', '.join(missing)}")

        events = normalized.get("events", [])
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

        event_by_id = {e.get("event_id"): e for e in events}
        block_by_id = {b["chunk_id"]: b for b in blocks_out}
        timeline_out: list[dict[str, Any]] = []
        for item in normalized.get("timeline", []):
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
                    detail = f"input={json_preview(e.get('input'), 500)}"
                    if e.get("error"):
                        detail += f" error={json_preview(e.get('error'), 500)}"
                    elif e.get("output") is not None:
                        detail += f" output={json_preview(e.get('output'), 500)}"
                else:
                    detail = json_preview({k: v for k, v in e.items() if k not in {"event_id", "type"}}, 700)
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

        status_counts = Counter(status_of(e) for e in tool_events)
        tool_counts = Counter(str(e.get("tool") or "unknown") for e in tool_events)
        session_map = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "source": manifest["source"],
            "prompts": manifest.get("prompts", []),
            "session_info": normalized.get("session_info"),
            "totals": normalized.get("totals", {}),
            "counts": {
                "messages": manifest["counts"]["messages"],
                "blocks": len(blocks_out),
                "events": len(events),
                "tool_calls": len(tool_events),
                "failed_tool_calls": len(failed),
                "summarized_blocks": summarized_count,
            },
            "tool_statistics": {
                "by_tool": dict(tool_counts),
                "by_status": dict(status_counts),
            },
            "deterministic_findings": {
                "permission_like_failures": [
                    {
                        "event_id": e.get("event_id"),
                        "message_id": e.get("message_id"),
                        "tool": e.get("tool"),
                        "input": e.get("input"),
                        "error": e.get("error"),
                    }
                    for e in permission_like
                ],
                "repeated_failed_actions": repeated,
                "retry_events": sum(1 for e in events if e.get("type") == "retry"),
                "subtask_events": sum(1 for e in events if e.get("type") == "subtask")
                + sum(1 for e in tool_events if str(e.get("tool") or "").lower() == "task"),
            },
            "blocks": blocks_out,
            "timeline": timeline_out,
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
            }
        )
    except Exception as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
