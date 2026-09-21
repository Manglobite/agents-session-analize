from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    compact_json,
    emit,
    fail,
    load_json,
    project_root,
    rel,
    safe_project_path,
    sha256_file,
    slugify,
    write_json,
)

SEMANTIC_KINDS = {"user_message", "assistant_response", "reasoning", "subtask_request"}


def message_id(info: dict[str, Any], index: int) -> str:
    return str(info.get("id") or f"message-{index:04d}")


def text_parts(parts: list[dict[str, Any]], *, include_synthetic: bool = False) -> list[str]:
    out: list[str] = []
    for part in parts:
        if part.get("type") != "text":
            continue
        if part.get("ignored") is True:
            continue
        if not include_synthetic and part.get("synthetic") is True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


def tool_event(part: dict[str, Any], msg_id: str, seq: int) -> dict[str, Any]:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = state.get("status") or "unknown"
    output = state.get("output")
    if output is None:
        output = state.get("result")
    event = {
        "event_id": f"e{seq:05d}",
        "type": "tool",
        "message_id": msg_id,
        "part_id": part.get("id"),
        "call_id": part.get("callID") or part.get("callId") or part.get("toolCallId"),
        "tool": part.get("tool") or part.get("toolName"),
        "status": status,
        "input": state.get("input") if "input" in state else part.get("input"),
        "output": output,
        "error": state.get("error"),
        "title": state.get("title"),
        "time": state.get("time"),
        "metadata": part.get("metadata"),
    }
    return event


def main() -> None:
    try:
        from common import read_input

        data = read_input()
        root = project_root(data)
        source = safe_project_path(root, str(data.get("session_path", "")))
        chunk_chars = int(data.get("chunk_chars") or 5000)
        if chunk_chars < 500 or chunk_chars > 200_000:
            raise ValueError("chunk_chars must be between 500 and 200000")

        exported = load_json(source)
        if not isinstance(exported, dict):
            raise ValueError("OpenCode export root must be a JSON object")
        messages = exported.get("messages")
        if not isinstance(messages, list):
            raise ValueError("OpenCode export must contain messages[]")

        session_hash = sha256_file(source)
        label = slugify(str(data.get("run_name") or source.stem), fallback="session")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"{stamp}-{label}-{session_hash[:8]}"
        run_dir = root / "runs" / run_id
        if run_dir.exists():
            raise ValueError(f"run already exists: {run_id}")

        for sub in ["raw/prompts", "normalized", "chunks", "summaries", "derived", "reports"]:
            (run_dir / sub).mkdir(parents=True, exist_ok=True)

        raw_session = run_dir / "raw" / "session-export.json"
        shutil.copy2(source, raw_session)

        prompt_records: list[dict[str, Any]] = []
        prompt_inputs: list[tuple[str, str]] = []
        orchestrator_prompt = data.get("orchestrator_prompt")
        if isinstance(orchestrator_prompt, str) and orchestrator_prompt.strip():
            prompt_inputs.append(("orchestrator", orchestrator_prompt))
        for idx, item in enumerate(data.get("subagent_prompts") or []):
            if isinstance(item, str) and item.strip():
                prompt_inputs.append((f"subagent-{idx+1:02d}", item))

        for role, raw_path in prompt_inputs:
            src = safe_project_path(root, raw_path)
            suffix = src.suffix if src.suffix else ".md"
            dst = run_dir / "raw" / "prompts" / f"{role}-{slugify(src.stem, fallback='prompt')}{suffix}"
            shutil.copy2(src, dst)
            prompt_records.append(
                {
                    "role": role,
                    "source_path": rel(root, src),
                    "run_path": rel(root, dst),
                    "sha256": sha256_file(dst),
                }
            )

        blocks: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        event_seq = 0

        def add_event(event: dict[str, Any]) -> None:
            nonlocal event_seq
            event_seq += 1
            if "event_id" not in event:
                event["event_id"] = f"e{event_seq:05d}"
            events.append(event)
            timeline.append({"type": "event", "ref": event["event_id"]})

        def add_block(kind: str, msg_id: str, content: str, source_refs: list[str], metadata: dict[str, Any]) -> None:
            if not content.strip():
                return
            ordinal = len(blocks) + 1
            block_id = f"b{ordinal:05d}-{kind}"
            needs_summary = kind in SEMANTIC_KINDS and len(content) > chunk_chars
            chunk_path = run_dir / "chunks" / f"{block_id}.json"
            record = {
                "chunk_id": block_id,
                "kind": kind,
                "message_id": msg_id,
                "char_count": len(content),
                "needs_summary": needs_summary,
                "chunk_path": rel(root, chunk_path),
                "source_refs": source_refs,
                "metadata": metadata,
            }
            blocks.append(record)
            write_json(
                chunk_path,
                {
                    **record,
                    "content": content,
                },
            )
            timeline.append({"type": "block", "ref": block_id})

        message_records: list[dict[str, Any]] = []
        totals = {"cost": 0.0, "tokens": {"input": 0.0, "output": 0.0, "reasoning": 0.0, "cache_read": 0.0, "cache_write": 0.0}}

        for msg_index, raw_msg in enumerate(messages, start=1):
            if not isinstance(raw_msg, dict):
                continue
            info = raw_msg.get("info") if isinstance(raw_msg.get("info"), dict) else {}
            parts_raw = raw_msg.get("parts")
            parts = [p for p in parts_raw if isinstance(p, dict)] if isinstance(parts_raw, list) else []
            msg_id = message_id(info, msg_index)
            role = str(info.get("role") or "unknown")
            msg_meta = {
                "id": msg_id,
                "role": role,
                "parent_id": info.get("parentID") or info.get("parentId"),
                "agent": info.get("agent") or info.get("mode"),
                "model_id": info.get("modelID") or (info.get("model") or {}).get("modelID") if isinstance(info.get("model"), dict) else info.get("modelID"),
                "provider_id": info.get("providerID") or (info.get("model") or {}).get("providerID") if isinstance(info.get("model"), dict) else info.get("providerID"),
                "time": info.get("time"),
                "cost": info.get("cost"),
                "tokens": info.get("tokens"),
                "part_count": len(parts),
            }
            message_records.append(msg_meta)

            if isinstance(info.get("cost"), (int, float)):
                totals["cost"] += float(info["cost"])
            tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
            for key in ["input", "output", "reasoning"]:
                if isinstance(tokens.get(key), (int, float)):
                    totals["tokens"][key] += float(tokens[key])
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            if isinstance(cache.get("read"), (int, float)):
                totals["tokens"]["cache_read"] += float(cache["read"])
            if isinstance(cache.get("write"), (int, float)):
                totals["tokens"]["cache_write"] += float(cache["write"])

            texts = text_parts(parts)
            if role == "user" and texts:
                add_block("user_message", msg_id, "\n\n".join(texts), [msg_id], msg_meta)
            elif role == "assistant" and texts:
                add_block("assistant_response", msg_id, "\n\n".join(texts), [msg_id], msg_meta)

            reasoning_index = 0
            for part in parts:
                ptype = part.get("type")
                if ptype == "reasoning":
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        reasoning_index += 1
                        add_block(
                            "reasoning",
                            msg_id,
                            text.strip(),
                            [str(part.get("id") or f"{msg_id}:reasoning:{reasoning_index}")],
                            {**msg_meta, "part_id": part.get("id"), "reasoning_index": reasoning_index},
                        )
                elif ptype == "tool":
                    event_seq += 1
                    event = tool_event(part, msg_id, event_seq)
                    events.append(event)
                    timeline.append({"type": "event", "ref": event["event_id"]})
                elif ptype == "subtask":
                    content = json.dumps(
                        {
                            "agent": part.get("agent"),
                            "description": part.get("description"),
                            "prompt": part.get("prompt"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    add_block(
                        "subtask_request",
                        msg_id,
                        content,
                        [str(part.get("id") or f"{msg_id}:subtask")],
                        {**msg_meta, "part_id": part.get("id"), "target_agent": part.get("agent")},
                    )
                    add_event(
                        {
                            "type": "subtask",
                            "message_id": msg_id,
                            "part_id": part.get("id"),
                            "agent": part.get("agent"),
                            "description": part.get("description"),
                        }
                    )
                elif ptype in {"retry", "compaction", "agent", "step-start", "step-finish", "patch", "snapshot", "file"}:
                    event = {
                        "type": str(ptype),
                        "message_id": msg_id,
                        "part_id": part.get("id"),
                    }
                    for key in ["attempt", "agent", "name", "reason", "hash", "files", "filename", "mime", "mediaType", "url", "auto", "error", "cost", "tokens"]:
                        if key in part:
                            event[key] = part.get(key)
                    add_event(event)

        normalized = {
            "session_info": exported.get("info"),
            "messages": message_records,
            "events": events,
            "timeline": timeline,
            "totals": totals,
            "source_sha256": session_hash,
        }
        write_json(run_dir / "normalized" / "session.json", normalized)
        write_json(run_dir / "normalized" / "events.json", events)

        pending = [
            {
                "chunk_id": b["chunk_id"],
                "kind": b["kind"],
                "chunk_path": b["chunk_path"],
                "char_count": b["char_count"],
            }
            for b in blocks
            if b["needs_summary"]
        ]

        evaluator_files = [
            root / ".opencode" / "tools" / "session_eval.ts",
            root / ".opencode" / "agents" / "session-evaluator.md",
            root / ".opencode" / "agents" / "trace-summarizer.md",
            root / ".opencode" / "agents" / "behavior-reviewer.md",
            root / "scripts" / "session_eval" / "common.py",
            root / "scripts" / "session_eval" / "prepare.py",
            root / "scripts" / "session_eval" / "store_summary.py",
            root / "scripts" / "session_eval" / "build_map.py",
            root / "scripts" / "session_eval" / "finalize.py",
        ]
        evaluator_fingerprint = {
            rel(root, path): sha256_file(path) for path in evaluator_files if path.is_file()
        }

        manifest = {
            "schema_version": 1,
            "versions": {
                "parser_schema": 1,
                "summary_schema": 1,
                "session_map_schema": 1,
                "review_schema": 1,
            },
            "evaluator_fingerprint": evaluator_fingerprint,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "original_path": rel(root, source),
                "run_copy": rel(root, raw_session),
                "sha256": session_hash,
            },
            "prompts": prompt_records,
            "config": {"chunk_chars": chunk_chars},
            "counts": {
                "messages": len(message_records),
                "events": len(events),
                "blocks": len(blocks),
                "pending_summaries": len(pending),
            },
            "blocks": blocks,
        }
        write_json(run_dir / "manifest.json", manifest)

        emit(
            {
                "ok": True,
                "run_id": run_id,
                "manifest_path": rel(root, run_dir / "manifest.json"),
                "counts": manifest["counts"],
                "pending_summaries": pending,
                "prompt_paths": [p["run_path"] for p in prompt_records],
                "warnings": [] if messages else ["session contains no messages"],
            }
        )
    except Exception as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
