from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare import (
    read_frontmatter,
    resolve_orchestrator,
    resolve_referenced_agents,
)

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = ROOT / "prompts" / "targets" / "ВОВКА_2"


class PreparePromptsTest(unittest.TestCase):
    def test_orchestrator_detects_primary_and_12_siblings(self) -> None:
        if not TARGET.is_dir():
            self.skipTest("ВОВКА_2 target directory not present")
        orch, subagents = resolve_orchestrator(ROOT, str(TARGET))
        self.assertEqual(orch.name, "DT.md")
        self.assertEqual(len(subagents), 12)

    def test_unrelated_markdown_is_not_an_agent(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "prompts") as tmp:
            directory = Path(tmp)
            (directory / "README.md").write_text("Documentation only")
            (directory / "orchestrator.md").write_text("---\nmode: primary\n---\nOrchestrate")
            (directory / "worker.md").write_text("---\nmode: subagent\n---\nWork")
            orchestrator, subagents = resolve_orchestrator(ROOT, str(directory))
            self.assertEqual(orchestrator.name, "orchestrator.md")
            self.assertEqual([p.name for p in subagents], ["worker.md"])

    def test_referenced_agents_dedup_to_12(self) -> None:
        if not TARGET.is_dir():
            self.skipTest("ВОВКА_2 target directory not present")
        orch, _ = resolve_orchestrator(ROOT, str(TARGET))
        found, unmatched, ambiguous = resolve_referenced_agents(ROOT, orch)
        self.assertEqual(len(found), 12)
        self.assertEqual(unmatched, [])
        self.assertEqual(ambiguous, [])
        paths = {f["source_path"] for f in found}
        self.assertEqual(len(paths), 12)

    def test_prompt_building_yields_13_unique(self) -> None:
        if not TARGET.is_dir():
            self.skipTest("ВОВКА_2 target directory not present")
        orch, dir_subagents = resolve_orchestrator(ROOT, str(TARGET))
        prompt_inputs = [("orchestrator", orch)]
        for sub_path in dir_subagents:
            prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))
        ref_agents, _, _ = resolve_referenced_agents(ROOT, orch)
        for ref_agent in ref_agents:
            sub_path = ROOT / ref_agent["source_path"]
            prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))

        seen: set[Path] = set()
        deduped: list[tuple[str, Path]] = []
        for role, src in prompt_inputs:
            resolved = src.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append((role, src))

        self.assertEqual(len(deduped), 13)
        self.assertEqual(deduped[0][0], "orchestrator")
        names = [read_frontmatter(p).get("name") for _, p in deduped[1:]]
        self.assertTrue(all(names))
        self.assertEqual(len(set(names)), 12)

    def test_full_real_scenario_with_explicit_subagent_prompts(self) -> None:
        if not TARGET.is_dir():
            self.skipTest("ВОВКА_2 target directory not present")
        orch, dir_subagents = resolve_orchestrator(ROOT, str(TARGET))
        prompt_inputs = [("orchestrator", orch)]
        for sub_path in dir_subagents:
            prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))
        for sub_path in dir_subagents:
            prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))
        ref_agents, _, _ = resolve_referenced_agents(ROOT, orch)
        for ref_agent in ref_agents:
            sub_path = ROOT / ref_agent["source_path"]
            prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))

        seen: set[Path] = set()
        deduped: list[tuple[str, Path]] = []
        for role, src in prompt_inputs:
            resolved = src.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append((role, src))

        self.assertEqual(len(deduped), 13)
        self.assertEqual(deduped[0][0], "orchestrator")
        names = [read_frontmatter(p).get("name") for _, p in deduped[1:]]
        self.assertEqual(len(set(names)), 12)


if __name__ == "__main__":
    unittest.main()
