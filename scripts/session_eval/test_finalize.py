from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from finalize import label, render, validate_add_agent_recommendation, validate_minimal_prompt_changes, validate_report_language


class FinalizeReportTest(unittest.TestCase):
    def test_russian_report_and_prompt_names(self) -> None:
        prompts = [
            {"run_path": "runs/r/raw/prompts/orchestrator-dt.md", "source_path": "agents/DT.md", "canonical_name": "ВОВКА"},
            {"run_path": "runs/r/raw/prompts/subagent-worker.md", "source_path": "agents/worker.md"},
        ]
        review = {
            "overall_summary": "Задача выполнена частично.",
            "prompt_contract": [{"rule_id": "R1", "strength": "hard", "actor": "ВОВКА", "rule": "Проверить доступ.", "source_prompt": "DT.md"}],
            "deviations": [{"deviation_id": "D1", "classification": "explicit_violation", "observed_behavior": "Запрет пропущен.", "rule_ids": ["R1"], "knowledge_state": "knowledge_available_but_ignored", "confidence": "high", "likely_reason": "Спешка.", "evidence": ["e00001"]}],
            "recoveries": [{"classification": "immediate_recovery", "related_deviation_id": "D1", "user_intervention_required": False, "evidence": ["e00002"]}],
            "user_interventions": [{"message_id": "m1", "classification": "correction", "effect": "Изменил направление.", "evidence": ["m1"]}],
            "prompt_findings": [{"classification": "instruction_ambiguous", "confidence": "medium", "description": "Нужна конкретика."}],
            "minimal_prompt_changes": [
                {"target_prompt": prompts[0]["run_path"], "no_change": False, "change": "Уточнить условие.", "why_minimal": "Одна строка."},
                {"target_prompt": prompts[1]["source_path"], "no_change": True, "change": "", "why_minimal": "Сбоя нет."},
            ],
            "add_agent_recommendation": {"decision": "no", "role": "дополнительный агент", "trigger": "n/a", "inputs": [], "outputs": [], "reason": "n/a", "evidence": ["e00001"], "why_existing_agents_cannot_cover": "n/a"},
        }
        report = render(review, "r", prompts)
        for phrase in ["## Краткий итог", "## Правила из промтов", "### D1 · прямое нарушение", "Доступность сведений: сведения были доступны, но не учтены", "быстрое восстановление", "исправление", "неоднозначная инструкция", "### ВОВКА", "### worker", "Изменения не требуются.", "**Вывод: нет — дополнительный сабагент не нужен.**", "Дополнительный сабагент не нужен:", "Доказательства:"]:
            self.assertIn(phrase, report)
        for phrase in ["## Summary", "Why minimal:", "Evidence:", "explicit_violation", "immediate_recovery", "n/a", "### runs/r/"]:
            self.assertNotIn(phrase, report)
        self.assertEqual(label("unexpected_value"), "unexpected value")
        self.assertIn("карта не показывает, что отдельный агент сократил бы передачи", render({**review, "prompt_findings": [{"description": "Карта не показывает, что отдельный агент сократил бы передачи."}]}, "r", prompts).lower())

    def test_recommendation_summary_from_reviewer(self) -> None:
        review = {"overall_summary": "", "prompt_contract": [], "deviations": [], "recoveries": [], "user_interventions": [], "prompt_findings": [], "minimal_prompt_changes": [], "add_agent_recommendation": {"decision": "yes", "role": "Аналитик", "trigger": "частые переключения", "inputs": [], "outputs": [], "reason": "", "summary": "Повторяющиеся передачи контекста требуют выделенной роли.", "evidence": [], "why_existing_agents_cannot_cover": "нет владельца"}}
        report = render(review, "r")
        self.assertIn("Повторяющиеся передачи контекста требуют выделенной роли.", report)
        self.assertIn("**Вывод: да — добавить сабагента.**", report)
        validate_add_agent_recommendation(review)

    def test_empty_recommendation_summary_is_rejected(self) -> None:
        recommendation = {"decision": "no", "role": None, "trigger": "", "inputs": [], "outputs": [], "reason": "", "summary": "n/a", "evidence": [], "why_existing_agents_cannot_cover": ""}
        with self.assertRaisesRegex(ValueError, "must explain"):
            validate_add_agent_recommendation({"add_agent_recommendation": recommendation})

    def test_english_review_is_rejected(self) -> None:
        review = {"overall_summary": "The orchestrator finished the task and handed work to the existing team.", "prompt_contract": [{"rule": "The agent must review the output before handoff."}], "deviations": [], "user_interventions": [], "prompt_findings": [], "minimal_prompt_changes": [{"change": "Add a new check before each handoff.", "why_minimal": "This is a small change to the existing workflow."}], "add_agent_recommendation": {"decision": "no", "summary": "The existing agents can handle this work without another specialist.", "reason": "", "trigger": "", "inputs": [], "outputs": [], "why_existing_agents_cannot_cover": ""}}
        with self.assertRaisesRegex(ValueError, "overall_summary.*prompt_contract.*minimal_prompt_changes.*add_agent_recommendation"):
            validate_report_language(review)

    def test_mixed_language_english_narrative_is_rejected(self) -> None:
        review = {"overall_summary": "Reported Библиотекарша unavailable and directed architectural analysis to Дедушка. The orchestrator switched on its next task call.", "prompt_contract": [], "deviations": [], "user_interventions": [], "prompt_findings": [], "minimal_prompt_changes": [], "add_agent_recommendation": {"decision": "no", "summary": "Дополнительный агент не нужен.", "reason": "", "trigger": "", "inputs": [], "outputs": [], "why_existing_agents_cannot_cover": ""}}
        with self.assertRaisesRegex(ValueError, "overall_summary"):
            validate_report_language(review)

    def test_russian_review_preserves_technical_terms(self) -> None:
        review = {"overall_summary": "Агент запустил mvn clean test в worktree и сообщил результат.", "prompt_contract": [], "deviations": [], "user_interventions": [], "prompt_findings": [], "minimal_prompt_changes": [{"change": "Проверяй `external_directory: deny` до запуска Bash.", "why_minimal": "Меняется только условие запуска."}], "add_agent_recommendation": {"decision": "no", "summary": "Отдельный агент не требуется: уже есть исполнитель и ревьюер.", "reason": "", "trigger": "n/a", "inputs": [], "outputs": [], "why_existing_agents_cannot_cover": "n/a"}}
        validate_report_language(review)

    def test_finalize_does_not_publish_english_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "r"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"prompts": []}))
            review = {"overall_summary": "The orchestrator finished the work and reported the results.", "prompt_contract": [], "deviations": [], "recoveries": [], "user_interventions": [], "prompt_findings": [], "minimal_prompt_changes": [], "add_agent_recommendation": {"decision": "no", "role": None, "trigger": "", "inputs": [], "outputs": [], "reason": "", "evidence": [], "why_existing_agents_cannot_cover": ""}}
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("finalize.py"))], input=json.dumps({"project_root": str(root), "run_id": "r", "review_json": json.dumps(review)}), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("overall_summary", result.stdout)
            self.assertFalse((run / "reports" / "report.md").exists())

    def test_prompt_validation_uses_original_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "manifest.json").write_text(json.dumps({"prompts": [{"run_path": "runs/r/raw/prompts/agent.md", "source_path": "agents/agent.md"}]}))
            validate_minimal_prompt_changes({"minimal_prompt_changes": [{"target_prompt": "agents/agent.md", "change": "", "no_change": True, "why_minimal": "Нет сбоя."}]}, run)


if __name__ == "__main__":
    unittest.main()
