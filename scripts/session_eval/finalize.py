from __future__ import annotations

import json
from typing import Any

from common import emit, fail, project_root, read_input, rel, safe_run_dir, write_json, write_text

REQUIRED_ARRAYS = [
    "prompt_contract",
    "deviations",
    "recoveries",
    "user_interventions",
    "prompt_findings",
    "minimal_prompt_changes",
]


def bullet_evidence(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    return "\n".join(f"  - {item}" for item in values)


def render(review: dict[str, Any], run_id: str) -> str:
    lines: list[str] = [f"# Agent session review — {run_id}", "", "## Summary", "", str(review.get("overall_summary", "")), ""]

    lines += ["## Prompt contract", ""]
    for rule in review.get("prompt_contract", []):
        lines.append(f"- **{rule.get('rule_id', '?')}** [{rule.get('strength', '?')}] `{rule.get('actor', '?')}` — {rule.get('rule', '')} _(source: {rule.get('source_prompt', '?')})_")
    if not review.get("prompt_contract"):
        lines.append("- No explicit rules extracted.")
    lines.append("")

    lines += ["## Deviations", ""]
    for item in review.get("deviations", []):
        lines.append(f"### {item.get('deviation_id', '?')} · {item.get('classification', 'other')}")
        lines.append("")
        lines.append(str(item.get("observed_behavior", "")))
        lines.append("")
        lines.append(f"- Rules: {', '.join(item.get('rule_ids', [])) or 'n/a'}")
        lines.append(f"- Knowledge state: {item.get('knowledge_state', 'n/a')}")
        lines.append(f"- Confidence: {item.get('confidence', 'n/a')}")
        if item.get("likely_reason"):
            lines.append(f"- Likely reason: {item['likely_reason']}")
        ev = bullet_evidence(item.get("evidence"))
        if ev:
            lines.append("- Evidence:")
            lines.append(ev)
        lines.append("")
    if not review.get("deviations"):
        lines.append("No deviations identified from available evidence.\n")

    lines += ["## Recoveries", ""]
    for item in review.get("recoveries", []):
        lines.append(
            f"- `{item.get('classification', 'unknown')}`; deviation={item.get('related_deviation_id')}; user intervention={item.get('user_intervention_required')}"
        )
    if not review.get("recoveries"):
        lines.append("- None identified.")
    lines.append("")

    lines += ["## User interventions", ""]
    for item in review.get("user_interventions", []):
        lines.append(f"- `{item.get('message_id', '?')}` **{item.get('classification', 'unknown')}** — {item.get('effect', '')}")
    if not review.get("user_interventions"):
        lines.append("- None identified.")
    lines.append("")

    lines += ["## Prompt findings", ""]
    for item in review.get("prompt_findings", []):
        lines.append(f"- **{item.get('classification', 'other')}** ({item.get('confidence', 'n/a')}): {item.get('description', '')}")
    if not review.get("prompt_findings"):
        lines.append("- None identified.")
    lines.append("")

    lines += ["## Minimal prompt changes", ""]
    for item in review.get("minimal_prompt_changes", []):
        lines.append(f"### {item.get('target_prompt', '?')}")
        lines.append("")
        lines.append(str(item.get("change", "")))
        lines.append("")
        lines.append(f"Addresses: {', '.join(item.get('addresses', [])) or 'n/a'}")
        lines.append("")
        lines.append(f"Why minimal: {item.get('why_minimal', '')}")
        lines.append("")
    if not review.get("minimal_prompt_changes"):
        lines.append("No prompt changes proposed.\n")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    try:
        data = read_input()
        root = project_root(data)
        run_dir = safe_run_dir(root, str(data.get("run_id", "")))
        raw = data.get("review_json")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("review_json is empty")
        try:
            review = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"review_json is not valid JSON: {exc}") from exc
        if not isinstance(review, dict):
            raise ValueError("review_json must decode to an object")
        if not isinstance(review.get("overall_summary"), str):
            raise ValueError("missing or invalid overall_summary")
        for key in REQUIRED_ARRAYS:
            if not isinstance(review.get(key), list):
                raise ValueError(f"missing or invalid array: {key}")

        json_path = run_dir / "reports" / "review.json"
        md_path = run_dir / "reports" / "report.md"
        write_json(json_path, {"schema_version": 1, **review})
        write_text(md_path, render(review, str(data.get("run_id"))))
        emit(
            {
                "ok": True,
                "run_id": data.get("run_id"),
                "review_json_path": rel(root, json_path),
                "report_path": rel(root, md_path),
            }
        )
    except Exception as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
