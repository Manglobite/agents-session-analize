from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import emit, fail, load_json, project_root, read_input, rel, safe_run_dir, write_json, write_text

REQUIRED_ARRAYS = [
    "prompt_contract",
    "deviations",
    "recoveries",
    "user_interventions",
    "prompt_findings",
    "minimal_prompt_changes",
]

ADD_AGENT_DECISIONS = {"yes", "no", "insufficient_evidence"}
MAX_EVIDENCE = 20
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
ENGLISH_WORD_RE = re.compile(r"\b(?:the|this|that|with|without|before|after|from|into|there|these|those|should|must|were|was|have|has|had|would|could|does|did|not|and|but|for|when|where|which|while|because|instead|only|another|existing|agent|agents|work|task|prompt|review|result|session|reported|observed|change|changes|evidence|user|file|files)\b", re.IGNORECASE)
TECHNICAL_RE = re.compile(r"`[^`]*`|https?://\S+|(?:[\w./-]+/)+[\w./-]+|\b(?:[A-Z][A-Z0-9_-]{2,}|[a-z]+(?:_[a-z0-9]+)+|[a-z]+\.[a-z0-9.-]+)\b")
LABELS = {
    "hard": "обязательное", "preferred": "предпочтительное", "informational": "информационное",
    "explicit_violation": "прямое нарушение", "ignored_constraint": "проигнорированное ограничение",
    "missed_preferred_path": "пропущенный предпочтительный путь", "capability_mismatch": "несоответствие возможностей",
    "unnecessary_attempt": "лишняя попытка", "goal_drift": "отклонение от цели", "other": "другое",
    "knowledge_missing": "сведения отсутствовали", "knowledge_available_but_ignored": "сведения были доступны, но не учтены",
    "knowledge_ambiguous": "сведения были неоднозначны", "not_applicable": "не применимо",
    "immediate_recovery": "быстрое восстановление", "delayed_recovery": "отложенное восстановление",
    "repeated_failure": "повторный сбой", "no_recovery": "без восстановления",
    "new_requirement": "новое требование", "clarification": "уточнение", "answer_to_agent": "ответ агенту",
    "correction": "исправление", "repetition_of_existing_instruction": "повтор ранее данного указания",
    "task_change": "изменение задачи", "instruction_absent": "инструкция отсутствует",
    "instruction_ambiguous": "неоднозначная инструкция", "instruction_conflicting": "противоречивые инструкции",
    "instruction_too_weak": "недостаточно строгое указание",
    "instruction_available_but_ignored": "инструкция была доступна, но не учтена",
    "capability_description_misleading": "неточное описание возможностей",
    "role_separation_problem": "неясное разделение ролей",
    "high": "высокая", "medium": "средняя", "low": "низкая",
    "yes": "да — добавить сабагента", "no": "нет — дополнительный сабагент не нужен",
    "insufficient_evidence": "недостаточно данных для решения", "orchestrator": "оркестратор",
    "all": "все агенты",
}


def label(value: Any) -> str:
    if value is None:
        return "не указано"
    return LABELS.get(str(value), str(value).replace("_", " "))


def bullet_evidence(values: Any, limit: int = MAX_EVIDENCE) -> str:
    if not isinstance(values, list) or not values:
        return ""
    items = [str(item) for item in values[:limit]]
    lines = "\n".join(f"  - {item}" for item in items)
    if len(values) > limit:
        lines += f"\n  - … и ещё {len(values) - limit}"
    return lines


def validate_minimal_prompt_changes(review: dict[str, Any], run_dir: Any) -> None:
    changes = review.get("minimal_prompt_changes")
    if not isinstance(changes, list):
        raise ValueError("missing or invalid minimal_prompt_changes")
    manifest = load_json(run_dir / "manifest.json")
    prompts = [p for p in manifest.get("prompts", []) if isinstance(p, dict)]
    if not prompts:
        if changes:
            raise ValueError("minimal_prompt_changes must be empty when no prompts are provided")
        return
    seen_ids: set[int] = set()
    for item in changes:
        if not isinstance(item, dict):
            raise ValueError("minimal_prompt_changes entries must be objects")
        target = item.get("target_prompt")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("minimal_prompt_changes entry missing target_prompt")
        matched = None
        for idx, prompt in enumerate(prompts):
            if target == prompt.get("run_path") or target == prompt.get("source_path"):
                matched = idx
                break
        if matched is None:
            raise ValueError(f"minimal_prompt_changes target_prompt not among provided prompts: {target}")
        if matched in seen_ids:
            raise ValueError(f"minimal_prompt_changes duplicate target_prompt: {target}")
        seen_ids.add(matched)
        if not isinstance(item.get("change"), str):
            raise ValueError("minimal_prompt_changes entry missing change")
        if not isinstance(item.get("no_change"), bool):
            raise ValueError("minimal_prompt_changes entry missing no_change")
        if not isinstance(item.get("why_minimal"), str):
            raise ValueError("minimal_prompt_changes entry missing why_minimal")
    if len(seen_ids) != len(prompts):
        raise ValueError("minimal_prompt_changes must include exactly one entry per provided prompt")


def validate_add_agent_recommendation(review: dict[str, Any]) -> None:
    recommendation = review.get("add_agent_recommendation")
    if not isinstance(recommendation, dict):
        raise ValueError("missing or invalid add_agent_recommendation")
    decision = recommendation.get("decision")
    if decision not in ADD_AGENT_DECISIONS:
        raise ValueError("add_agent_recommendation.decision must be one of: yes, no, insufficient_evidence")
    role = recommendation.get("role")
    if decision == "no":
        if role is not None and not (isinstance(role, str) and role.strip()):
            raise ValueError("add_agent_recommendation.role must be a non-empty string or null when decision is no")
    elif not isinstance(role, str) or not role.strip():
        raise ValueError("add_agent_recommendation.role must be a non-empty string")
    if not isinstance(recommendation.get("trigger"), str):
        raise ValueError("add_agent_recommendation.trigger must be a string")
    for key in ("inputs", "outputs"):
        value = recommendation.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"add_agent_recommendation.{key} must be an array of strings")
    if not isinstance(recommendation.get("reason"), str):
        raise ValueError("add_agent_recommendation.reason must be a string")
    if "summary" in recommendation and not isinstance(recommendation["summary"], str):
        raise ValueError("add_agent_recommendation.summary must be a string")
    if "summary" in recommendation and recommendation["summary"].strip().lower() in {"", "n/a", "не применимо"}:
        raise ValueError("add_agent_recommendation.summary must explain the decision")
    evidence = recommendation.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ValueError("add_agent_recommendation.evidence must be an array of strings")
    if not isinstance(recommendation.get("why_existing_agents_cannot_cover"), str):
        raise ValueError("add_agent_recommendation.why_existing_agents_cannot_cover must be a string")


def validate_report_language(review: dict[str, Any]) -> None:
    fields = ["overall_summary"]
    prose: list[tuple[str, Any]] = [(field, review.get(field)) for field in fields]
    for section, keys in {
        "prompt_contract": ["rule"],
        "deviations": ["observed_behavior", "likely_reason"],
        "user_interventions": ["effect"],
        "prompt_findings": ["description"],
        "minimal_prompt_changes": ["change", "why_minimal"],
    }.items():
        for index, item in enumerate(review.get(section, [])):
            if isinstance(item, dict):
                prose.extend((f"{section}[{index}].{key}", item.get(key)) for key in keys)
    recommendation = review.get("add_agent_recommendation", {})
    for key in ("summary", "reason", "trigger", "why_existing_agents_cannot_cover"):
        prose.append((f"add_agent_recommendation.{key}", recommendation.get(key)))
    for key in ("inputs", "outputs"):
        for index, value in enumerate(recommendation.get(key, [])):
            prose.append((f"add_agent_recommendation.{key}[{index}]", value))
    english_only: list[str] = []
    for field, value in prose:
        if not isinstance(value, str) or not value.strip() or value.strip().lower() in {"n/a", "none"}:
            continue
        visible = TECHNICAL_RE.sub(" ", value)
        latin_words = LATIN_WORD_RE.findall(visible)
        english_words = ENGLISH_WORD_RE.findall(visible)
        cyrillic_chars = len(CYRILLIC_RE.findall(visible))
        if (len(latin_words) >= 4 and cyrillic_chars == 0) or (len(english_words) >= 3 and len(english_words) >= cyrillic_chars / 5):
            english_only.append(field)
    if english_only:
        raise ValueError("Текст отчёта должен быть на русском языке; перепишите поля: " + ", ".join(english_only))


def prompt_name(target: str, prompts: list[dict[str, Any]]) -> str:
    for prompt in prompts:
        if target in {prompt.get("run_path"), prompt.get("source_path")}:
            return str(prompt.get("canonical_name") or Path(str(prompt.get("source_path") or target)).stem)
    return Path(target).stem


def recommendation_summary(item: dict[str, Any], findings: list[dict[str, Any]] | None = None) -> str:
    summary = str(item.get("summary") or "").strip()
    if summary and summary.lower() not in {"n/a", "не применимо"}:
        return summary
    decision = item.get("decision")
    if decision == "no":
        for finding in findings or []:
            description = str(finding.get("description") or "").strip()
            if "отдельный агент" in description.lower() or "новый агент" in description.lower():
                return f"Дополнительный сабагент не нужен. {description}"
        return "Дополнительный сабагент не нужен: в этой сессии не выявлена отдельная повторяющаяся роль, для которой добавление агента было бы обосновано."
    if decision == "yes":
        return f"Рекомендуется добавить сабагента для роли «{item.get('role') or 'не указана'}»."
    return "Недостаточно данных, чтобы обоснованно решить, нужен ли дополнительный сабагент."


def render(review: dict[str, Any], run_id: str, prompts: list[dict[str, Any]] | None = None) -> str:
    prompts = prompts or []
    lines: list[str] = [f"# Анализ сессии агентов — {run_id}", "", "## Краткий итог", "", str(review.get("overall_summary", "")), ""]

    lines += ["## Правила из промтов", ""]
    for rule in review.get("prompt_contract", []):
        lines.append(f"- **{rule.get('rule_id', '?')}** [{label(rule.get('strength'))}] `{label(rule.get('actor'))}` — {rule.get('rule', '')} _(источник: {rule.get('source_prompt', '?')})_")
    if not review.get("prompt_contract"):
        lines.append("- Явные правила не выделены.")
    lines.append("")

    lines += ["## Отклонения", ""]
    for item in review.get("deviations", []):
        lines += [f"### {item.get('deviation_id', '?')} · {label(item.get('classification'))}", "", str(item.get("observed_behavior", "")), ""]
        lines.append(f"- Правила: {', '.join(item.get('rule_ids', [])) or 'не указаны'}")
        lines.append(f"- Доступность сведений: {label(item.get('knowledge_state'))}")
        lines.append(f"- Уверенность: {label(item.get('confidence'))}")
        if item.get("likely_reason"):
            lines.append(f"- Возможная причина: {item['likely_reason']}")
        evidence = bullet_evidence(item.get("evidence"))
        if evidence:
            lines += ["- Доказательства:", evidence]
        lines.append("")
    if not review.get("deviations"):
        lines.append("По доступным данным отклонения не выявлены.\n")

    lines += ["## Восстановление после ошибок", ""]
    for item in review.get("recoveries", []):
        intervention = "да" if item.get("user_intervention_required") else "нет"
        lines.append(f"- **{label(item.get('classification'))}**; отклонение: {item.get('related_deviation_id') or 'не указано'}; вмешательство пользователя: {intervention}")
        evidence = bullet_evidence(item.get("evidence"))
        if evidence:
            lines += ["  - Доказательства:", "\n".join(f"    - {ref}" for ref in item["evidence"][:MAX_EVIDENCE])]
            if len(item["evidence"]) > MAX_EVIDENCE:
                lines.append(f"    - … и ещё {len(item['evidence']) - MAX_EVIDENCE}")
    if not review.get("recoveries"):
        lines.append("- Не выявлено.")
    lines.append("")

    lines += ["## Вмешательства пользователя", ""]
    for item in review.get("user_interventions", []):
        lines.append(f"- `{item.get('message_id', '?')}` **{label(item.get('classification'))}** — {item.get('effect', '')}")
        evidence = bullet_evidence(item.get("evidence"))
        if evidence:
            lines += ["  - Доказательства:", "\n".join(f"    - {ref}" for ref in item["evidence"][:MAX_EVIDENCE])]
            if len(item["evidence"]) > MAX_EVIDENCE:
                lines.append(f"    - … и ещё {len(item['evidence']) - MAX_EVIDENCE}")
    if not review.get("user_interventions"):
        lines.append("- Не выявлено.")
    lines.append("")

    lines += ["## Выводы о промтах", ""]
    for item in review.get("prompt_findings", []):
        lines.append(f"- **{label(item.get('classification'))}** (уверенность: {label(item.get('confidence'))}): {item.get('description', '')}")
    if not review.get("prompt_findings"):
        lines.append("- Не выявлено.")
    lines.append("")

    lines += ["## Минимальные изменения промтов", ""]
    for item in review.get("minimal_prompt_changes", []):
        lines += [f"### {prompt_name(str(item.get('target_prompt', '')), prompts)}", ""]
        if item.get("no_change"):
            lines.append("Изменения не требуются.")
        else:
            lines.append(str(item.get("change", "")))
        lines += ["", f"Почему это минимальное изменение: {item.get('why_minimal', '')}", ""]
    if not review.get("minimal_prompt_changes"):
        lines.append("Изменения промтов не предложены.\n")

    add_agent = review.get("add_agent_recommendation", {})
    lines += ["## Рекомендация о дополнительном сабагенте", "", f"**Вывод: {label(add_agent.get('decision'))}.**", "", recommendation_summary(add_agent, review.get("prompt_findings")), ""]
    if add_agent.get("decision") == "yes":
        lines.append(f"- Роль: {add_agent.get('role') or 'не указана'}")
        lines.append(f"- Когда вызывать: {add_agent.get('trigger') or 'не указано'}")
        lines.append(f"- Входные данные: {', '.join(add_agent.get('inputs', [])) or 'не указаны'}")
        lines.append(f"- Результат работы: {', '.join(add_agent.get('outputs', [])) or 'не указан'}")
        lines.append(f"- Почему не подходят существующие агенты: {add_agent.get('why_existing_agents_cannot_cover') or 'не указано'}")
    reason = str(add_agent.get("reason") or "").strip()
    if reason and reason.lower() not in {"n/a", "не применимо"}:
        lines.append(f"- Обоснование: {reason}")
    evidence = bullet_evidence(add_agent.get("evidence"))
    if evidence:
        lines += ["- Доказательства:", evidence]
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
        validate_minimal_prompt_changes(review, run_dir)
        validate_add_agent_recommendation(review)
        validate_report_language(review)

        json_path = run_dir / "reports" / "review.json"
        md_path = run_dir / "reports" / "report.md"
        write_json(json_path, {"schema_version": 1, **review})
        manifest = load_json(run_dir / "manifest.json")
        write_text(md_path, render(review, str(data.get("run_id")), manifest.get("prompts", [])))
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
