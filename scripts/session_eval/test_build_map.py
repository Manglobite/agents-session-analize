from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from build_map import (
    analyze_session,
    build_delegation,
    build_recoveries,
    extract_child_session_id,
    normalize_sessions,
    redact,
    redact_preview,
)


def make_run(root: Path, run_id: str, normalized: dict, manifest_blocks: list[dict] | None = None) -> Path:
    run_dir = root / "runs" / run_id
    for sub in ["raw/prompts", "normalized", "chunks", "summaries", "derived", "reports"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "source": {"original_path": "x", "run_copy": "y", "sha256": "z"},
        "prompts": [],
        "counts": {"messages": 3, "events": 5, "blocks": 1, "pending_summaries": 0},
        "blocks": manifest_blocks or [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "normalized" / "session.json").write_text(json.dumps(normalized))
    return run_dir


def ev(sid: str, eid: str, typ: str, tool: str | None = None, status: str | None = None, **kw) -> dict:
    d = {"event_id": eid, "type": typ, "session_id": sid}
    if tool:
        d["tool"] = tool
    if status:
        d["status"] = status
    d.update(kw)
    return d


class BuildMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normalize_sessions_list(self) -> None:
        normalized = {
            "session_info": {"id": "ses_root"},
            "sessions": [
                {"session_id": "ses_root", "session_info": {"id": "ses_root"}, "events": [], "timeline": [], "totals": {}, "messages": [], "blocks": []},
                {"session_id": "ses_child", "session_info": {"id": "ses_child"}, "events": [], "timeline": [], "totals": {}, "messages": [], "blocks": []},
            ],
        }
        views, mode = normalize_sessions(normalized)
        self.assertEqual(mode, "sessions_list")
        self.assertEqual(len(views), 2)

    def test_normalize_single(self) -> None:
        normalized = {"session_info": {"id": "ses_root"}, "events": [], "timeline": [], "totals": {}, "messages": [], "blocks": []}
        views, mode = normalize_sessions(normalized)
        self.assertEqual(mode, "single")
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["session_id"], "ses_root")

    def test_normalize_combined(self) -> None:
        normalized = {
            "session_info": {"id": "ses_root"},
            "events": [
                ev("ses_root", "e1", "tool", "read", "completed"),
                ev("ses_child", "e2", "tool", "read", "completed"),
            ],
            "timeline": [],
            "totals": {},
            "messages": [],
            "blocks": [],
        }
        views, mode = normalize_sessions(normalized)
        self.assertEqual(mode, "combined")
        self.assertEqual({v["session_id"] for v in views}, {"ses_root", "ses_child"})

    def test_extract_child_session_id(self) -> None:
        e = ev("r", "e1", "tool", "task", "completed", metadata={"sessionId": "ses_child"})
        self.assertEqual(extract_child_session_id(e), "ses_child")
        e2 = ev("r", "e2", "tool", "task", "completed", output='<task id="ses_abc" state="completed">')
        self.assertEqual(extract_child_session_id(e2), "ses_abc")
        e3 = ev("r", "e3", "tool", "task", "completed", input={"task_id": "ses_xyz"})
        self.assertEqual(extract_child_session_id(e3), "ses_xyz")

    def test_analyze_session_tool_stats_and_findings(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_root",
            "session_info": {"id": "ses_root"},
            "events": [
                ev("ses_root", "e1", "tool", "read", "error", error="File not found"),
                ev("ses_root", "e2", "tool", "read", "completed"),
                ev("ses_root", "e3", "tool", "read", "error", error="permission denied"),
                ev("ses_root", "e4", "retry"),
            ],
            "timeline": [{"type": "event", "ref": "e1"}, {"type": "event", "ref": "e2"}, {"type": "event", "ref": "e3"}, {"type": "event", "ref": "e4"}],
            "totals": {},
            "messages": [],
            "blocks": [],
        }
        out = analyze_session(view, is_root=True, run_dir=run_dir)
        self.assertEqual(out["counts"]["tool_calls"], 3)
        self.assertEqual(out["counts"]["failed_tool_calls"], 2)
        self.assertEqual(out["tool_statistics"]["by_tool"], {"read": 3})
        self.assertEqual(out["tool_statistics"]["by_status"], {"error": 2, "completed": 1})
        self.assertEqual(out["deterministic_findings"]["retry_events"], 1)
        self.assertEqual(len(out["deterministic_findings"]["permission_like_failures"]), 1)
        self.assertEqual(len(out["deterministic_findings"]["repeated_failed_actions"]), 1)

    def test_build_delegation_and_recoveries(self) -> None:
        views = [
            {
                "session_id": "ses_root",
                "events": [
                    ev("ses_root", "e1", "tool", "task", "error", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                    ev("ses_root", "e2", "tool", "task", "completed", input={"subagent_type": "B"}, metadata={"sessionId": "ses_child2"}),
                ],
            },
            {"session_id": "ses_child1", "events": []},
            {"session_id": "ses_child2", "events": []},
        ]
        delegation = build_delegation(views)
        self.assertEqual(delegation["task_calls"], 2)
        self.assertEqual(delegation["children_in_scope"], 2)
        self.assertEqual(delegation["children_out_of_scope"], 0)

        recoveries = build_recoveries(views)
        self.assertEqual(recoveries["delegation_recoveries"], [])
        self.assertEqual(recoveries["tool_recoveries"], [])
        self.assertEqual(len(recoveries["delegation_fallbacks"]), 1)
        self.assertEqual(recoveries["delegation_fallbacks"][0]["classification"], "fallback")

    def test_build_recoveries_tool_and_delegation(self) -> None:
        views = [
            {
                "session_id": "ses_root",
                "events": [
                    ev("ses_root", "e1", "tool", "read", "error"),
                    ev("ses_root", "e2", "tool", "read", "completed"),
                    ev("ses_root", "e3", "tool", "task", "error", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                    ev("ses_root", "e4", "tool", "task", "completed", input={"subagent_type": "B"}, metadata={"sessionId": "ses_child2"}),
                ],
            },
            {"session_id": "ses_child1", "events": []},
            {"session_id": "ses_child2", "events": []},
        ]
        recoveries = build_recoveries(views)
        self.assertEqual(len(recoveries["tool_recoveries"]), 1)
        self.assertEqual(recoveries["tool_recoveries"][0]["tool"], "read")
        self.assertEqual(len(recoveries["delegation_recoveries"]), 0)
        self.assertEqual(len(recoveries["delegation_fallbacks"]), 1)
        self.assertEqual(recoveries["delegation_fallbacks"][0]["failed_subagent_type"], "A")
        self.assertEqual(recoveries["delegation_fallbacks"][0]["recovered_subagent_type"], "B")

    def test_redact_sensitive_keys_and_patterns(self) -> None:
        value = {
            "filePath": "/tmp/x",
            "password": "hunter2",
            "api_key": "sk-abcdefghijklmnop",
            "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz123456"},
            "body": "token=secretvalue123 and AKIAABCDEFGHIJKLMNOP",
            "nested": [{"secret": "s3cr3t", "ok": "fine"}],
        }
        out = redact(value)
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertEqual(out["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(out["nested"][0]["secret"], "[REDACTED]")
        self.assertEqual(out["nested"][0]["ok"], "fine")
        self.assertNotIn("sk-abcdefghijklmnop", out["body"])
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", out["body"])
        self.assertNotIn("secretvalue123", out["body"])
        self.assertIn("[REDACTED]", out["body"])

    def test_redact_preview_bounds_and_marks_truncation(self) -> None:
        big = {"data": "x" * 2000}
        preview = redact_preview(big, 100)
        self.assertIn("[truncated", preview)
        self.assertLess(len(preview), 200)

    def test_findings_redact_tool_input(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_root",
            "session_info": {"id": "ses_root"},
            "events": [
                ev("ses_root", "e1", "tool", "read", "error", error="permission denied", input={"filePath": "/x", "password": "hunter2"}),
            ],
            "timeline": [{"type": "event", "ref": "e1"}],
            "totals": {},
            "messages": [],
            "blocks": [],
        }
        out = analyze_session(view, is_root=True, run_dir=run_dir)
        pf = out["deterministic_findings"]["permission_like_failures"][0]
        self.assertNotIn("hunter2", pf["input"])
        self.assertIn("[REDACTED]", pf["input"])

    def test_child_missing_summary_fails(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_child",
            "session_info": {"id": "ses_child"},
            "events": [],
            "timeline": [{"type": "block", "ref": "ses_child-b00001-assistant_response"}],
            "totals": {},
            "messages": [],
            "blocks": [
                {
                    "chunk_id": "ses_child-b00001-assistant_response",
                    "kind": "assistant_response",
                    "message_id": "m1",
                    "char_count": 9000,
                    "needs_summary": True,
                    "content": "x" * 9000,
                    "source_refs": ["m1"],
                    "metadata": {},
                }
            ],
        }
        with self.assertRaises(ValueError):
            analyze_session(view, is_root=False, run_dir=run_dir)

    def test_manifest_child_block_missing_summary_fails(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_child",
            "session_info": {"id": "ses_child"},
            "events": [],
            "timeline": [],
            "totals": {},
            "messages": [],
            "blocks": [
                {
                    "chunk_id": "ses_child-b00001-assistant_response",
                    "kind": "assistant_response",
                    "message_id": "m1",
                    "char_count": 9000,
                    "needs_summary": True,
                    "content": "x" * 9000,
                    "source_refs": ["m1"],
                    "metadata": {},
                }
            ],
        }
        with self.assertRaises(ValueError):
            analyze_session(view, is_root=False, run_dir=run_dir)

    def test_observed_agents(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_root",
            "session_info": {"id": "ses_root"},
            "events": [],
            "timeline": [],
            "totals": {},
            "messages": [
                {"id": "m1", "agent": "ВОВКА"},
                {"id": "m2", "agent": "Библиотекарша"},
                {"id": "m3", "agent": "ВОВКА"},
            ],
            "blocks": [],
        }
        out = analyze_session(view, is_root=True, run_dir=run_dir)
        self.assertEqual(out["observed_agents"], ["Библиотекарша", "ВОВКА"])

    def test_block_verbatim_and_metadata_redacted(self) -> None:
        run_dir = make_run(self.root, "r1", {})
        view = {
            "session_id": "ses_root",
            "session_info": {"id": "ses_root"},
            "events": [],
            "timeline": [{"type": "block", "ref": "b00001-assistant_response"}],
            "totals": {},
            "messages": [],
            "blocks": [
                {
                    "chunk_id": "b00001-assistant_response",
                    "kind": "assistant_response",
                    "message_id": "m1",
                    "char_count": 10,
                    "needs_summary": False,
                    "content": "token=secretvalue123 and AKIAABCDEFGHIJKLMNOP",
                    "source_refs": ["m1"],
                    "metadata": {"password": "hunter2", "ok": "fine"},
                }
            ],
        }
        out = analyze_session(view, is_root=True, run_dir=run_dir)
        block = out["blocks"][0]
        self.assertNotIn("secretvalue123", block["summary_text"])
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", block["summary_text"])
        self.assertIn("[REDACTED]", block["summary_text"])
        self.assertNotIn("secretvalue123", block["representation"]["content"])
        self.assertEqual(block["metadata"]["password"], "[REDACTED]")
        self.assertEqual(block["metadata"]["ok"], "fine")
        self.assertNotIn("secretvalue123", out["timeline"][0]["summary_text"])

    def test_build_recoveries_delegation_retry_same_subagent(self) -> None:
        views = [
            {
                "session_id": "ses_root",
                "events": [
                    ev("ses_root", "e1", "tool", "task", "error", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                    ev("ses_root", "e2", "tool", "task", "completed", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                ],
            },
            {"session_id": "ses_child1", "events": []},
        ]
        recoveries = build_recoveries(views)
        self.assertEqual(len(recoveries["delegation_recoveries"]), 1)
        self.assertEqual(recoveries["delegation_recoveries"][0]["classification"], "retry")
        self.assertEqual(recoveries["delegation_recoveries"][0]["failed_subagent_type"], "A")
        self.assertEqual(recoveries["delegation_recoveries"][0]["recovered_subagent_type"], "A")
        self.assertEqual(len(recoveries["delegation_fallbacks"]), 0)

    def test_build_recoveries_delegation_scoped_per_session(self) -> None:
        views = [
            {
                "session_id": "ses_parent1",
                "events": [
                    ev("ses_parent1", "p1e1", "tool", "task", "error", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                ],
            },
            {
                "session_id": "ses_parent2",
                "events": [
                    ev("ses_parent2", "p2e1", "tool", "task", "completed", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                ],
            },
            {"session_id": "ses_child1", "events": []},
        ]
        recoveries = build_recoveries(views)
        self.assertEqual(recoveries["delegation_recoveries"], [])
        self.assertEqual(recoveries["delegation_fallbacks"], [])
        self.assertEqual(recoveries["total"], 0)

    def test_build_recoveries_delegation_retry_same_child_id(self) -> None:
        views = [
            {
                "session_id": "ses_root",
                "events": [
                    ev("ses_root", "e1", "tool", "task", "error", input={"subagent_type": "A"}, metadata={"sessionId": "ses_child1"}),
                    ev("ses_root", "e2", "tool", "task", "completed", input={"subagent_type": "B"}, metadata={"sessionId": "ses_child1"}),
                ],
            },
            {"session_id": "ses_child1", "events": []},
        ]
        recoveries = build_recoveries(views)
        self.assertEqual(len(recoveries["delegation_recoveries"]), 1)
        self.assertEqual(recoveries["delegation_recoveries"][0]["classification"], "retry")
        self.assertEqual(recoveries["delegation_recoveries"][0]["failed_child_session_id"], "ses_child1")
        self.assertEqual(recoveries["delegation_recoveries"][0]["recovered_child_session_id"], "ses_child1")
        self.assertEqual(len(recoveries["delegation_fallbacks"]), 0)

    def test_main_end_to_end_multi_session(self) -> None:
        run_id = "r1"
        run_dir = make_run(self.root, run_id, {})
        root_sid = "ses_root"
        child_sid = "ses_child"
        root_view = {
            "session_id": root_sid,
            "session_info": {"id": root_sid},
            "events": [
                ev(root_sid, "e00001", "tool", "task", "completed", input={"subagent_type": "A"}, metadata={"sessionId": child_sid}),
                ev(root_sid, "e00002", "tool", "read", "error", error="permission denied"),
                ev(root_sid, "e00003", "tool", "read", "completed"),
            ],
            "timeline": [
                {"type": "event", "ref": "e00001"},
                {"type": "event", "ref": "e00002"},
                {"type": "event", "ref": "e00003"},
            ],
            "totals": {"cost": 1.0, "tokens": {"input": 10.0, "output": 5.0, "reasoning": 0.0, "cache_read": 0.0, "cache_write": 0.0}},
            "messages": [{"id": "m1", "agent": "ВОВКА"}],
            "blocks": [],
        }
        child_view = {
            "session_id": child_sid,
            "session_info": {"id": child_sid},
            "events": [ev(child_sid, "ses_child-e00001", "tool", "glob", "completed")],
            "timeline": [{"type": "event", "ref": "ses_child-e00001"}],
            "totals": {"cost": 0.5, "tokens": {"input": 3.0, "output": 2.0, "reasoning": 0.0, "cache_read": 0.0, "cache_write": 0.0}},
            "messages": [{"id": "m2", "agent": "Библиотекарша"}],
            "blocks": [],
        }
        normalized = {
            "session_info": {"id": root_sid},
            "sessions": [root_view, child_view],
        }
        (run_dir / "normalized" / "session.json").write_text(json.dumps(normalized))
        manifest = json.loads((run_dir / "manifest.json").read_text())
        manifest["session_tree"] = {
            "root_session_id": root_sid,
            "child_session_copies": [{"session_id": child_sid}],
            "missing_child_exports": ["ses_missing"],
            "ambiguous_child_exports": [{"session_id": "ses_ambig", "candidates": ["a.json", "b.json"]}],
            "missing_agent_prompts": ["agent-x.md"],
            "ambiguous_agent_prompts": [],
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest))

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "build_map.py")],
            input=json.dumps({"project_root": str(self.root), "run_id": run_id}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["multi_session"]["session_count"], 2)
        self.assertEqual(out["session_tree"]["missing_child_exports"], ["ses_missing"])
        self.assertEqual(len(out["session_tree"]["ambiguous_child_exports"]), 1)
        self.assertEqual(out["delegation"]["task_calls"], 1)
        self.assertEqual(out["delegation"]["children_in_scope"], 1)
        self.assertEqual(out["recoveries"]["tool_recoveries"], 1)
        self.assertEqual(len(out["deterministic_findings"]["permission_like_failures"]), 1)

        session_map = json.loads((run_dir / "derived" / "session-map.json").read_text())
        self.assertEqual(session_map["counts"]["tool_calls"], 3)
        self.assertEqual(session_map["totals"]["cost"], 1.0)
        self.assertEqual(len(session_map["sessions"]), 2)
        child = next(s for s in session_map["sessions"] if s["session_id"] == child_sid)
        self.assertEqual(child["observed_agents"], ["Библиотекарша"])
        self.assertEqual(child["tool_statistics"]["by_tool"], {"glob": 1})
        self.assertEqual(child["totals"]["cost"], 0.5)

    def test_main_fails_when_child_copy_missing_from_normalized(self) -> None:
        run_id = "r1"
        run_dir = make_run(self.root, run_id, {})
        root_sid = "ses_root"
        child_sid = "ses_child"
        root_view = {
            "session_id": root_sid,
            "session_info": {"id": root_sid},
            "events": [],
            "timeline": [],
            "totals": {},
            "messages": [],
            "blocks": [],
        }
        normalized = {"session_info": {"id": root_sid}, "sessions": [root_view]}
        (run_dir / "normalized" / "session.json").write_text(json.dumps(normalized))
        manifest = json.loads((run_dir / "manifest.json").read_text())
        manifest["session_tree"] = {
            "root_session_id": root_sid,
            "child_session_copies": [{"session_id": child_sid}],
            "missing_child_exports": [],
            "ambiguous_child_exports": [],
            "missing_agent_prompts": [],
            "ambiguous_agent_prompts": [],
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest))

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "build_map.py")],
            input=json.dumps({"project_root": str(self.root), "run_id": run_id}),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertFalse(out["ok"])
        self.assertIn(child_sid, out["error"])


if __name__ == "__main__":
    unittest.main()
