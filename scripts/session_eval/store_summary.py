from __future__ import annotations

import json
from typing import Any

from common import emit, fail, load_json, project_root, read_input, rel, safe_chunk_id, safe_run_dir, write_json

SCHEMAS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "user_message": {
        "summary": str,
        "intent": (str, type(None)),
        "added_constraints": list,
        "changed_constraints": list,
        "correction_of_previous_behavior": (str, type(None)),
        "references_previous_goal": bool,
    },
    "reasoning": {
        "summary": str,
        "current_goal": (str, type(None)),
        "hypotheses": list,
        "decision": (str, type(None)),
        "decision_reason": (str, type(None)),
        "observations": list,
        "next_action": (str, type(None)),
    },
    "assistant_response": {
        "summary": str,
        "claims": list,
        "reported_results": list,
        "unresolved": list,
        "asks_user": list,
    },
    "subtask_request": {
        "summary": str,
        "assigned_goal": (str, type(None)),
        "target_agent": (str, type(None)),
        "expected_result": (str, type(None)),
    },
}


def validate_list_of_strings(name: str, value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")


def main() -> None:
    try:
        data = read_input()
        root = project_root(data)
        run_dir = safe_run_dir(root, str(data.get("run_id", "")))
        chunk_id = safe_chunk_id(str(data.get("chunk_id", "")))
        manifest = load_json(run_dir / "manifest.json")
        block = next((b for b in manifest.get("blocks", []) if b.get("chunk_id") == chunk_id), None)
        if not block:
            raise ValueError(f"unknown chunk_id: {chunk_id}")
        if not block.get("needs_summary"):
            raise ValueError(f"chunk does not require summary: {chunk_id}")

        raw = data.get("summary_json")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("summary_json is empty")
        try:
            summary = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"summary_json is not valid JSON: {exc}") from exc
        if not isinstance(summary, dict):
            raise ValueError("summary_json must decode to an object")

        kind = str(block.get("kind"))
        schema = SCHEMAS.get(kind)
        if not schema:
            raise ValueError(f"no summary schema for kind: {kind}")

        errors: list[str] = []
        for key, expected in schema.items():
            if key not in summary:
                errors.append(f"missing field: {key}")
                continue
            if not isinstance(summary[key], expected):
                errors.append(f"wrong type for {key}")
        for key in [k for k, t in schema.items() if t is list]:
            if key in summary:
                try:
                    validate_list_of_strings(key, summary[key])
                except ValueError as exc:
                    errors.append(str(exc))
        if isinstance(summary.get("summary"), str) and not summary["summary"].strip():
            errors.append("summary must not be empty")
        if errors:
            raise ValueError("; ".join(errors))

        payload = {
            "schema_version": 1,
            "chunk_id": chunk_id,
            "kind": kind,
            "source_refs": block.get("source_refs", []),
            "summary": summary,
        }
        output = run_dir / "summaries" / f"{chunk_id}.json"
        write_json(output, payload)
        emit({"ok": True, "run_id": manifest["run_id"], "chunk_id": chunk_id, "summary_path": rel(root, output)})
    except Exception as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
