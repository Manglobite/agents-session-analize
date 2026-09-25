from __future__ import annotations

import io
from contextlib import redirect_stdout
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare import (
    RootSelectionError,
    build_session_tree,
    collect_child_session_ids,
    collect_unresolved_tasks,
    ensure_child_export,
    export_session_via_cli,
    find_existing_export,
    main,
    rescue_markdown_export,
    resolve_root_source,
)


def make_export(session_id: str) -> dict:
    return {"info": {"id": session_id}, "messages": []}


class CollectChildSessionIdsTest(unittest.TestCase):
    def test_metadata_session_id(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "metadata": {"sessionId": "ses_child1"},
                            },
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), {"ses_child1"})

    def test_task_id_input(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "input": {"task_id": "ses_child2"},
                            },
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), {"ses_child2"})

    def test_output_synthetic_completion(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "output": '<task id="ses_child3" state="completed"><task_result>ok</task_result>',
                            },
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), {"ses_child3"})

    def test_ignores_non_task_and_invalid_ids(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {"type": "tool", "state": {"tool": "read", "metadata": {"sessionId": "ses_x"}}},
                        {"type": "tool", "state": {"tool": "task", "metadata": {"sessionId": "not-an-id"}}},
                        {"type": "tool", "state": {"tool": "task", "input": {"task_id": "ses_ok"}}},
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), {"ses_ok"})

    def test_synthetic_text_background_completion_referenced_by_task(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "metadata": {"sessionId": "ses_bg"},
                            },
                        }
                    ]
                },
                {
                    "parts": [
                        {
                            "type": "text",
                            "synthetic": True,
                            "text": '<task id="ses_bg" state="completed">\n<task_result>done</task_result>',
                        }
                    ]
                },
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), {"ses_bg"})

    def test_synthetic_text_unknown_id_not_injected(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "text",
                            "synthetic": True,
                            "text": '<task id="ses_arbitrary" state="completed">\n<task_result>done</task_result>',
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), set())

    def test_non_synthetic_text_not_scanned(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "text",
                            "text": '<task id="ses_user" state="completed">\n<task_result>done</task_result>',
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_child_session_ids(exported), set())


class ExportSessionViaCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rejects_invalid_session_id(self) -> None:
        with self.assertRaises(ValueError):
            export_session_via_cli("not-a-session-id", cwd=self.root)

    def _run_cli(self, session_id: str, payload: bytes) -> Path:
        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stderr = b""
            return proc

        with mock.patch("prepare.subprocess.run", side_effect=fake_run) as run:
            target = export_session_via_cli(session_id, cwd=self.root)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["opencode", "--pure", "export", session_id])
        self.assertEqual(kwargs["cwd"], str(self.root))
        return target

    def test_returns_valid_export(self) -> None:
        target = self._run_cli("ses_abc", json.dumps(make_export("ses_abc")).encode())
        data = json.loads(target.read_text())
        self.assertEqual(data["info"]["id"], "ses_abc")
        self.assertTrue(target.is_file())
        target.unlink(missing_ok=True)
        self.assertFalse(target.exists())

    def test_rejects_mismatched_id(self) -> None:
        with self.assertRaises(ValueError):
            self._run_cli("ses_abc", json.dumps(make_export("ses_other")).encode())


class FindExistingExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "sessions" / "raw"
        self.session_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_finds_by_info_id(self) -> None:
        (self.session_dir / "a.json").write_text(json.dumps(make_export("ses_abc")))
        matches = find_existing_export(self.session_dir, "ses_abc")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "a.json")

    def test_finds_by_named_file(self) -> None:
        (self.session_dir / "ses_abc.json").write_text(json.dumps(make_export("ses_abc")))
        matches = find_existing_export(self.session_dir, "ses_abc")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "ses_abc.json")

    def test_ignores_mismatched_named_file(self) -> None:
        (self.session_dir / "ses_abc.json").write_text(json.dumps(make_export("ses_other")))
        self.assertEqual(find_existing_export(self.session_dir, "ses_abc"), [])

    def test_does_not_scan_global_sessions_raw(self) -> None:
        global_raw = self.root / "sessions" / "raw"
        global_raw.mkdir(parents=True, exist_ok=True)
        (global_raw / "ses_abc.json").write_text(json.dumps(make_export("ses_abc")))
        other_dir = self.root / "other"
        other_dir.mkdir()
        self.assertEqual(find_existing_export(other_dir, "ses_abc"), [])


class EnsureChildExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "sessions" / "raw"
        self.session_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prefers_existing_export(self) -> None:
        (self.session_dir / "a.json").write_text(json.dumps(make_export("ses_abc")))
        missing: list[str] = []
        ambiguous: list[dict] = []
        path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertEqual(path.name, "a.json")
        self.assertEqual(missing, [])
        self.assertEqual(ambiguous, [])

    def test_reports_ambiguous(self) -> None:
        (self.session_dir / "a.json").write_text(json.dumps(make_export("ses_abc")))
        (self.session_dir / "b.json").write_text(json.dumps(make_export("ses_abc")))
        missing: list[str] = []
        ambiguous: list[dict] = []
        path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertIsNone(path)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["session_id"], "ses_abc")

    def test_cli_fallback_writes_named_file(self) -> None:
        payload = json.dumps(make_export("ses_abc")).encode()

        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stderr = b""
            return proc

        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.subprocess.run", side_effect=fake_run):
            path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertEqual(path.name, "ses_abc.json")
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.session_dir)
        self.assertEqual(missing, [])
        self.assertEqual(ambiguous, [])

    def test_cli_failure_reports_gap(self) -> None:
        proc = mock.Mock()
        proc.returncode = 1
        proc.stderr = b"session not found"
        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.subprocess.run", return_value=proc):
            path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertIsNone(path)
        self.assertEqual(missing, ["ses_abc"])
        self.assertEqual(ambiguous, [])

    def test_cli_rejects_mismatched_parent_id(self) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix="session-export-", suffix=".json")
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps({"info": {"id": "ses_abc", "parentID": "ses_other"}, "messages": []}).encode())
        tmp_path = Path(tmp_name)

        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.export_session_via_cli", return_value=tmp_path):
            path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertIsNone(path)
        self.assertEqual(missing, ["ses_abc"])
        self.assertEqual(ambiguous, [])
        self.assertFalse(tmp_path.exists(), "temp file must be cleaned up on parent mismatch")

    def test_existing_export_mismatched_parent_reports_ambiguous(self) -> None:
        existing = self.session_dir / "ses_abc.json"
        existing.write_text(json.dumps({"info": {"id": "ses_abc", "parentID": "ses_other"}, "messages": []}))
        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.export_session_via_cli") as exported:
            path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        exported.assert_not_called()
        self.assertIsNone(path)
        self.assertEqual(missing, [])
        self.assertEqual(ambiguous[0]["reason"], "existing export has mismatched parent session id")

    def test_existing_target_mismatched_id_reports_ambiguous(self) -> None:
        (self.session_dir / "ses_abc.json").write_text(json.dumps(make_export("ses_other")))
        payload = json.dumps(make_export("ses_abc")).encode()

        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stderr = b""
            return proc

        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.subprocess.run", side_effect=fake_run):
            path = ensure_child_export(self.root, self.session_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertIsNone(path)
        self.assertEqual(missing, [])
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["session_id"], "ses_abc")
        self.assertIn("mismatched", ambiguous[0]["reason"])

    def test_writes_to_passed_session_dir_not_global(self) -> None:
        custom_dir = self.root / "custom-sessions"
        custom_dir.mkdir()
        payload = json.dumps(make_export("ses_abc")).encode()

        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stderr = b""
            return proc

        missing: list[str] = []
        ambiguous: list[dict] = []
        with mock.patch("prepare.subprocess.run", side_effect=fake_run):
            path = ensure_child_export(self.root, custom_dir, "ses_abc", missing=missing, ambiguous=ambiguous, parent_id="ses_root", cwd=self.root)
        self.assertEqual(path.parent, custom_dir)
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "sessions" / "raw" / "ses_abc.json").exists())
        self.assertEqual(missing, [])
        self.assertEqual(ambiguous, [])


class CollectUnresolvedTasksTest(unittest.TestCase):
    def test_failed_task_without_id_noted(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "status": "error", "metadata": {}},
                        }
                    ]
                }
            ]
        }
        unresolved = collect_unresolved_tasks(exported, "ses_root")
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["parent_session_id"], "ses_root")
        self.assertEqual(unresolved[0]["status"], "error")

    def test_failed_task_with_id_not_noted(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "status": "error",
                                "metadata": {"sessionId": "ses_child"},
                            },
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_unresolved_tasks(exported, "ses_root"), [])

    def test_completed_task_not_noted(self) -> None:
        exported = {
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "status": "completed", "metadata": {}},
                        }
                    ]
                }
            ]
        }
        self.assertEqual(collect_unresolved_tasks(exported, "ses_root"), [])


class BuildSessionTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "sessions" / "raw"
        self.raw.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, sid: str, child_ids: list[str] | None = None) -> Path:
        parts = []
        for cid in child_ids or []:
            parts.append(
                {
                    "type": "tool",
                    "state": {"tool": "task", "metadata": {"sessionId": cid}},
                }
            )
        p = self.raw / name
        p.write_text(json.dumps({"info": {"id": sid}, "messages": [{"parts": parts}]}))
        return p

    def test_duplicate_explicit_id_reports_ambiguous(self) -> None:
        root_path = self._write("root.json", "ses_root", ["ses_child"])
        a = self._write("a.json", "ses_child")
        b = self._write("b.json", "ses_child")
        records, missing, ambiguous, edges, unresolved = build_session_tree(
            self.root, root_path, [a, b], self.root, self.raw
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["session_id"], "ses_child")
        self.assertEqual(len(ambiguous[0]["candidates"]), 2)

    def test_cycle_edge_recorded(self) -> None:
        root_path = self._write("root.json", "ses_root", ["ses_child"])
        child = self._write("child.json", "ses_child", ["ses_root"])
        records, missing, ambiguous, edges, unresolved = build_session_tree(
            self.root, root_path, [child], self.root, self.raw
        )
        self.assertEqual(len(records), 2)
        cycle_edges = [e for e in edges if e["kind"] == "cycle_or_duplicate"]
        self.assertEqual(len(cycle_edges), 1)
        self.assertEqual(cycle_edges[0]["parent_session_id"], "ses_child")
        self.assertEqual(cycle_edges[0]["child_session_id"], "ses_root")

    def test_parent_child_edges_recorded(self) -> None:
        root_path = self._write("root.json", "ses_root", ["ses_child"])
        child = self._write("child.json", "ses_child")
        records, missing, ambiguous, edges, unresolved = build_session_tree(
            self.root, root_path, [child], self.root, self.raw
        )
        self.assertEqual(len(records), 2)
        parent_child = [e for e in edges if e["kind"] == "parent_child"]
        self.assertEqual(len(parent_child), 1)
        self.assertEqual(parent_child[0]["parent_session_id"], "ses_root")
        self.assertEqual(parent_child[0]["child_session_id"], "ses_child")


class RescueMarkdownExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "sessions" / "raw"
        self.session_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_saves_json_alongside_md_and_preserves_md(self) -> None:
        md = self.session_dir / "root.md"
        md.write_text("**Session ID:** ses_root\n")
        payload = json.dumps(make_export("ses_root")).encode()

        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stderr = b""
            return proc

        with mock.patch("prepare.subprocess.run", side_effect=fake_run):
            target, sid = rescue_markdown_export(md, self.root)
        self.assertEqual(sid, "ses_root")
        self.assertEqual(target.name, "ses_root.json")
        self.assertEqual(target.parent, self.session_dir)
        self.assertTrue(target.exists())
        self.assertTrue(md.exists(), "original MD must be preserved")

    def test_reuses_existing_json_same_id(self) -> None:
        md = self.session_dir / "root.md"
        md.write_text("**Session ID:** ses_root\n")
        (self.session_dir / "existing.json").write_text(json.dumps(make_export("ses_root")))
        with mock.patch("prepare.subprocess.run") as run:
            target, sid = rescue_markdown_export(md, self.root)
        run.assert_not_called()
        self.assertEqual(target.name, "existing.json")
        self.assertEqual(sid, "ses_root")

    def test_rejects_conflicting_named_json(self) -> None:
        md = self.session_dir / "root.md"
        md.write_text("**Session ID:** ses_root\n")
        foreign = self.session_dir / "ses_root.json"
        foreign.write_text(json.dumps(make_export("ses_other")))
        payload = json.dumps(make_export("ses_root")).encode()

        def fake_run(cmd, stdout, stderr, timeout, cwd):
            stdout.write(payload)
            return mock.Mock(returncode=0, stderr=b"")

        with mock.patch("prepare.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                rescue_markdown_export(md, self.root)
        self.assertEqual(json.loads(foreign.read_text())["info"]["id"], "ses_other")


class ResolveRootSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "sessions" / "raw"
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_path_returned_directly(self) -> None:
        p = self.dir / "a.json"
        p.write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        self.assertEqual(resolve_root_source(self.root, str(p)), p)

    def test_directory_prefers_json_over_md_same_id(self) -> None:
        (self.dir / "root.md").write_text("**Session ID:** ses_root\n")
        (self.dir / "root.json").write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        result = resolve_root_source(self.root, str(self.dir))
        self.assertEqual(result.name, "root.json")

    def test_directory_prefers_root_json_by_parent_id_absence(self) -> None:
        (self.dir / "root.json").write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        (self.dir / "child.json").write_text(json.dumps({"info": {"id": "ses_child", "parentID": "ses_root"}, "messages": []}))
        result = resolve_root_source(self.root, str(self.dir))
        self.assertEqual(result.name, "root.json")

    def test_directory_multiple_json_raises_with_candidates(self) -> None:
        (self.dir / "a.json").write_text(json.dumps({"info": {"id": "ses_a"}, "messages": []}))
        (self.dir / "b.json").write_text(json.dumps({"info": {"id": "ses_b"}, "messages": []}))
        with self.assertRaises(RootSelectionError) as ctx:
            resolve_root_source(self.root, str(self.dir))
        self.assertEqual(len(ctx.exception.candidates), 2)
        self.assertTrue(any("a.json" in c for c in ctx.exception.candidates))
        self.assertTrue(any("b.json" in c for c in ctx.exception.candidates))

    def test_directory_single_md_fallback(self) -> None:
        (self.dir / "root.md").write_text("**Session ID:** ses_root\n")
        result = resolve_root_source(self.root, str(self.dir))
        self.assertEqual(result.name, "root.md")

    def test_directory_multiple_md_no_json_raises(self) -> None:
        (self.dir / "a.md").write_text("**Session ID:** ses_a\n")
        (self.dir / "b.md").write_text("**Session ID:** ses_b\n")
        with self.assertRaises(RootSelectionError) as ctx:
            resolve_root_source(self.root, str(self.dir))
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_directory_no_usable_files_raises(self) -> None:
        (self.dir / "notes.txt").write_text("hello")
        with self.assertRaises(ValueError):
            resolve_root_source(self.root, str(self.dir))

    def test_directory_only_child_json_raises_no_root(self) -> None:
        (self.dir / "child.json").write_text(json.dumps({"info": {"id": "ses_child", "parentID": "ses_root"}, "messages": []}))
        with self.assertRaises(RootSelectionError):
            resolve_root_source(self.root, str(self.dir))

    def test_directory_root_plus_children_selects_root(self) -> None:
        (self.dir / "root.json").write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        (self.dir / "c1.json").write_text(json.dumps({"info": {"id": "ses_c1", "parentID": "ses_root"}, "messages": []}))
        (self.dir / "c2.json").write_text(json.dumps({"info": {"id": "ses_c2", "parentID": "ses_root"}, "messages": []}))
        result = resolve_root_source(self.root, str(self.dir))
        self.assertEqual(result.name, "root.json")

    def test_directory_root_plus_task_referenced_children_selects_root(self) -> None:
        root_export = {
            "info": {"id": "ses_root"},
            "messages": [
                {
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "metadata": {"sessionId": "ses_c1"}},
                        }
                    ]
                }
            ],
        }
        (self.dir / "root.json").write_text(json.dumps(root_export))
        (self.dir / "c1.json").write_text(json.dumps({"info": {"id": "ses_c1"}, "messages": []}))
        result = resolve_root_source(self.root, str(self.dir))
        self.assertEqual(result.name, "root.json")

    def test_directory_single_child_with_parent_id_reports_no_root(self) -> None:
        (self.dir / "child.json").write_text(json.dumps({"info": {"id": "ses_child", "parentID": "ses_root"}, "messages": []}))
        with self.assertRaises(RootSelectionError):
            resolve_root_source(self.root, str(self.dir))

    def test_directory_duplicate_ids_raises(self) -> None:
        (self.dir / "a.json").write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        (self.dir / "b.json").write_text(json.dumps({"info": {"id": "ses_root"}, "messages": []}))
        with self.assertRaises(RootSelectionError) as ctx:
            resolve_root_source(self.root, str(self.dir))
        self.assertEqual(len(ctx.exception.candidates), 2)


class MainDirectoryIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "sessions" / "raw" / "my-session"
        self.session_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_main_accepts_directory_and_writes_child_alongside_root(self) -> None:
        root_export = {
            "info": {"id": "ses_root"},
            "messages": [
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": "hello"}],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "tool": "task",
                                "metadata": {"sessionId": "ses_child"},
                            },
                        }
                    ],
                },
            ],
        }
        (self.session_dir / "root.json").write_text(json.dumps(root_export))
        (self.session_dir / "ses_child.json").write_text(
            json.dumps({"info": {"id": "ses_child", "parentID": "ses_root"}, "messages": []})
        )
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "prepare.py")],
            input=json.dumps({"project_root": str(self.root), "session_path": str(self.session_dir)}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        child_file = self.session_dir / "ses_child.json"
        self.assertTrue(child_file.exists(), "child export must be alongside root in session folder")
        self.assertFalse((self.root / "sessions" / "raw" / "ses_child.json").exists())


class RealFolderIntegrationTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent.parent
        self.folder = self.root / "sessions" / "raw" / "002"
        if not self.folder.is_dir():
            self.skipTest("sessions/raw/002 folder not present")

    def test_real_002_folder_same_folder_discovery_no_global_write(self) -> None:
        before = {p.name for p in self.folder.glob("*.json")}
        global_before = {p.name for p in (self.root / "sessions" / "raw").glob("*.json")}

        root_json = self.folder / "session-ses_f2cc.json"
        if not root_json.is_file():
            self.skipTest("sample root export not present")
        root_data = json.loads(root_json.read_text())
        child_ids = collect_child_session_ids(root_data)
        self.assertEqual(len(child_ids), 7, "sample root must reference exactly 7 child sessions")
        if not all((self.folder / f"{cid}.json").is_file() for cid in child_ids):
            self.skipTest("sample child exports not present")

        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "prepare.py")],
            input=json.dumps({
                "project_root": str(self.root),
                "session_path": str(self.folder),
                "run_name": f"test-002-{os.getpid()}-{time.time_ns()}",
            }),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + "\n" + proc.stdout)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        after = {p.name for p in self.folder.glob("*.json")}
        self.assertEqual(after, before)
        global_after = {p.name for p in (self.root / "sessions" / "raw").glob("*.json")}
        self.assertEqual(global_after, global_before)
        tree = out["session_tree"]
        self.assertEqual(tree["root_session_id"], "ses_f2cc8282dffejeHCM3M8XMaK8v")
        self.assertGreaterEqual(len(tree["sessions"]), 2)


class AutoExportIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "sessions" / "raw" / "my-session"
        self.session_dir.mkdir(parents=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.map_file = self.root / "opencode_map.json"
        self._write_fake_opencode()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_opencode(self) -> None:
        script = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if '--pure' in args:\n"
            "    args.remove('--pure')\n"
            "if args and args[0] == 'export' and len(args) >= 2:\n"
            "    sid = args[1]\n"
            "    with open(os.environ['FAKE_OPENCODE_MAP']) as f:\n"
            "        mapping = json.load(f)\n"
            "    if sid in mapping:\n"
            "        sys.stdout.write(json.dumps(mapping[sid]))\n"
            "        sys.exit(0)\n"
            "sys.stderr.write('session not found')\n"
            "sys.exit(1)\n"
        )
        exe = self.bin_dir / "opencode"
        exe.write_text(script)
        exe.chmod(0o755)

    def _set_map(self, mapping: dict[str, dict]) -> None:
        self.map_file.write_text(json.dumps(mapping))

    def _run_prepare(self, run_name: str) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["FAKE_OPENCODE_MAP"] = str(self.map_file)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "prepare.py")],
            input=json.dumps({
                "project_root": str(self.root),
                "session_path": str(self.session_dir),
                "run_name": run_name,
            }),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_nested_auto_export_adjacent_and_no_overwrite(self) -> None:
        root_export = {
            "info": {"id": "ses_root"},
            "messages": [
                {
                    "info": {"role": "user"},
                    "parts": [{"type": "text", "text": "hello"}],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "metadata": {"sessionId": "ses_child"}},
                        }
                    ],
                },
            ],
        }
        child_export = {
            "info": {"id": "ses_child", "parentID": "ses_root"},
            "messages": [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "metadata": {"sessionId": "ses_grandchild"}},
                        }
                    ],
                }
            ],
        }
        grandchild_export = {"info": {"id": "ses_grandchild", "parentID": "ses_child"}, "messages": []}
        (self.session_dir / "root.json").write_text(json.dumps(root_export))
        self._set_map({
            "ses_child": child_export,
            "ses_grandchild": grandchild_export,
        })

        out = self._run_prepare(f"nested-{os.getpid()}")

        child_file = self.session_dir / "ses_child.json"
        grandchild_file = self.session_dir / "ses_grandchild.json"
        self.assertTrue(child_file.exists(), "child must be written adjacent to root")
        self.assertTrue(grandchild_file.exists(), "grandchild must be written adjacent to root")
        self.assertFalse((self.root / "sessions" / "raw" / "ses_child.json").exists())
        self.assertFalse((self.root / "sessions" / "raw" / "ses_grandchild.json").exists())

        tree = out["session_tree"]
        session_ids = {s["session_id"] for s in tree["sessions"]}
        self.assertEqual(session_ids, {"ses_root", "ses_child", "ses_grandchild"})
        self.assertEqual(tree["root_session_id"], "ses_root")

    def test_no_overwrite_foreign_id_json(self) -> None:
        root_export = {
            "info": {"id": "ses_root"},
            "messages": [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "state": {"tool": "task", "metadata": {"sessionId": "ses_child"}},
                        }
                    ],
                }
            ],
        }
        (self.session_dir / "root.json").write_text(json.dumps(root_export))
        foreign = self.session_dir / "ses_child.json"
        foreign.write_text(json.dumps({"info": {"id": "ses_foreign", "parentID": "ses_root"}, "messages": []}))
        self._set_map({"ses_child": {"info": {"id": "ses_child", "parentID": "ses_root"}, "messages": []}})

        out = self._run_prepare(f"nooverwrite-{os.getpid()}")

        self.assertEqual(json.loads(foreign.read_text())["info"]["id"], "ses_foreign")
        tree = out["session_tree"]
        self.assertTrue(any(a["session_id"] == "ses_child" for a in tree["ambiguous_child_exports"]))

    def test_duplicate_run_id_safe(self) -> None:
        root_export = {"info": {"id": "ses_root"}, "messages": []}
        source = self.session_dir / "root.json"
        source.write_text(json.dumps(root_export))
        from common import sha256_file

        run_id = f"20260925-000000-dupe-{sha256_file(source)[:8]}"
        pre_run = self.root / "runs" / run_id
        pre_run.mkdir(parents=True)
        sentinel = pre_run / "manifest.json"
        sentinel.write_text("PRE-EXISTING")
        payload = json.dumps({"project_root": str(self.root), "session_path": str(self.session_dir), "run_name": "dupe"})
        output = io.StringIO()
        with mock.patch("prepare.datetime") as clock, mock.patch("sys.stdin", io.StringIO(payload)):
            clock.now.return_value.strftime.return_value = "20260925-000000"
            with redirect_stdout(output), self.assertRaises(SystemExit):
                main()
        self.assertIn("run already exists", output.getvalue())
        self.assertEqual(sentinel.read_text(), "PRE-EXISTING")


if __name__ == "__main__":
    unittest.main()
