from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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

AGENT_FILE_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|.*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FM_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
FM_MODE_RE = re.compile(r"^mode:\s*(.+?)\s*$", re.MULTILINE)
TASK_ALLOW_RE = re.compile(r'^["\']?([^"\':]+)["\']?\s*:\s*allow\s*$')
SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9_-]+$")
TASK_ID_OUTPUT_RE = re.compile(r'<task id="(ses_[A-Za-z0-9_-]+)"')


class RootSelectionError(Exception):
    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.candidates = candidates


def read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    body = match.group(1)
    fm: dict[str, str] = {}
    nm = FM_NAME_RE.search(body)
    if nm:
        fm["name"] = nm.group(1).strip()
    md = FM_MODE_RE.search(body)
    if md:
        fm["mode"] = md.group(1).strip().lower()
    return fm


def extract_task_allowlist(text: str) -> list[str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return []
    lines = match.group(1).splitlines()
    names: list[str] = []
    for i, line in enumerate(lines):
        if not re.match(r"^\s*task:\s*$", line):
            continue
        task_indent = len(line) - len(line.lstrip())
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip():
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= task_indent:
                break
            m = TASK_ALLOW_RE.match(nxt.strip())
            if m:
                names.append(m.group(1).strip())
        break
    return names


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


def tool_event(part: dict[str, Any], msg_id: str, seq: int, prefix: str = "") -> dict[str, Any]:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = state.get("status") or "unknown"
    output = state.get("output")
    if output is None:
        output = state.get("result")
    state_meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    part_meta = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
    merged_meta = {**part_meta}
    for key in ("sessionId", "sessionID", "parentSessionId", "parentSessionID"):
        if key in state_meta:
            merged_meta[key] = state_meta[key]
    event = {
        "event_id": f"{prefix}e{seq:05d}",
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
        "metadata": merged_meta,
        "state_metadata": state_meta,
        "child_session_id": state_meta.get("sessionId") or state_meta.get("sessionID"),
        "parent_session_id": state_meta.get("parentSessionId") or state_meta.get("parentSessionID"),
    }
    return event


SESSION_ID_IN_MD_RE = re.compile(r"\*\*Session ID:\*\*\s*(ses_[A-Za-z0-9_-]+)")


def rescue_markdown_export(source: Path, root: Path) -> tuple[Path, str]:
    text = source.read_text(encoding="utf-8", errors="replace")
    match = SESSION_ID_IN_MD_RE.search(text)
    if not match:
        raise ValueError(
            "Markdown export has no '**Session ID:**' header and cannot be re-exported automatically; "
            "run: opencode export <session-id> > sessions/raw/<file>.json"
        )
    session_id = match.group(1)
    session_dir = source.parent
    existing = find_existing_export(session_dir, session_id)
    if len(existing) == 1:
        return existing[0], session_id
    if len(existing) > 1:
        raise ValueError(
            f"session directory has multiple JSON exports for session {session_id}; "
            f"cannot pick a re-export target automatically"
        )
    tmp = export_session_via_cli(session_id, cwd=root)
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / f"{session_id}.json"
    if target.exists():
        tmp.unlink(missing_ok=True)
        try:
            data = load_json(target)
        except Exception as exc:
            raise ValueError(f"existing export is unreadable: {target}") from exc
        info = data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else {}
        if info.get("id") != session_id or not isinstance(data.get("messages"), list):
            raise ValueError(f"existing export conflicts with session {session_id}: {target}")
        return target, session_id
    os.replace(tmp, target)
    return target, session_id


def resolve_orchestrator(root: Path, raw: str) -> tuple[Path, list[Path]]:
    path = safe_project_path(root, raw)
    if path.is_file():
        return path, []
    top_level = sorted(p for p in path.glob("*.md") if p.is_file())
    if not top_level:
        raise ValueError(f"orchestrator_prompt directory contains no .md files: {raw}")
    explicit = path / "orchestrator.md"
    if explicit.is_file():
        orchestrator = explicit
    else:
        non_subagent = [
            p for p in top_level if read_frontmatter(p).get("mode") in {"all", "primary"}
        ]
        if len(non_subagent) == 1:
            orchestrator = non_subagent[0]
        elif len(non_subagent) > 1:
            raise ValueError(
                f"orchestrator_prompt directory has multiple primary agents; "
                f"cannot pick a primary automatically: {raw}"
            )
        elif len(top_level) == 1:
            orchestrator = top_level[0]
        else:
            raise ValueError(
                f"orchestrator_prompt directory has no orchestrator.md and no unique "
                f"non-subagent file; cannot pick a primary automatically: {raw}"
            )
    subagents = sorted(p for p in top_level if p != orchestrator and read_frontmatter(p).get("mode") == "subagent")
    subagents += sorted(p for p in path.glob("subagents/*.md") if p.is_file() and p.name.lower() != "readme.md")
    return orchestrator, subagents


def resolve_subagent(root: Path, raw: str) -> list[Path]:
    path = safe_project_path(root, raw)
    if path.is_file():
        return [path]
    resolved = sorted(p for p in path.glob("*.md") if p.is_file())
    if not resolved:
        raise ValueError(f"subagent prompt directory contains no .md files: {raw}")
    return resolved


def is_within_project(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_referenced_agents(
    root: Path, orchestrator_path: Path
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    text = orchestrator_path.read_text(encoding="utf-8", errors="replace")
    found: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    orch_dir = orchestrator_path.parent

    sibling_defs: dict[str, list[Path]] = {}
    for p in sorted(orch_dir.glob("*.md")):
        if p == orchestrator_path:
            continue
        fm = read_frontmatter(p)
        nm = fm.get("name")
        if nm:
            sibling_defs.setdefault(nm, []).append(p)
    for p in sorted(orch_dir.glob("subagents/*.md")):
        fm = read_frontmatter(p)
        nm = fm.get("name")
        if nm:
            sibling_defs.setdefault(nm, []).append(p)

    task_names = extract_task_allowlist(text)

    def resolve_ref(ref: str) -> None:
        candidates: list[Path] = []
        try:
            p = safe_project_path(root, ref, must_exist=False)
            if p.is_file():
                candidates.append(p)
        except ValueError:
            pass
        p2 = (orch_dir / ref).resolve()
        if p2.is_file() and is_within_project(root, p2):
            candidates.append(p2)
        p3 = (orch_dir / Path(ref).name).resolve()
        if p3.is_file() and is_within_project(root, p3):
            candidates.append(p3)
        p4 = (orch_dir / "subagents" / Path(ref).name).resolve()
        if p4.is_file() and is_within_project(root, p4):
            candidates.append(p4)

        unique: list[Path] = []
        for c in candidates:
            if c not in unique:
                unique.append(c)
        if len(unique) == 1:
            found.append(
                {
                    "canonical_name": ref,
                    "source_path": rel(root, unique[0]),
                }
            )
        elif len(unique) > 1:
            ambiguous.append(
                {
                    "reference": ref,
                    "candidates": [rel(root, c) for c in unique],
                }
            )
        else:
            unmatched.append(ref)

    table_refs: list[str] = []
    for line in text.splitlines():
        if not TABLE_ROW_RE.match(line):
            continue
        for ref in AGENT_FILE_REF_RE.findall(line):
            if ref in table_refs:
                continue
            base = Path(ref).name
            is_agent_path = "/agents/" in ref or ref.startswith("agents/") or "/subagents/" in ref or ref.startswith("subagents/")
            is_task_name = base in task_names
            if is_agent_path or is_task_name:
                table_refs.append(ref)
    for ref in table_refs:
        resolve_ref(ref)

    found_paths: set[str] = set()
    deduped_found: list[dict[str, Any]] = []
    for f in found:
        if f["source_path"] in found_paths:
            continue
        found_paths.add(f["source_path"])
        deduped_found.append(f)
    found = deduped_found

    for name in task_names:
        if any(f["canonical_name"] == name for f in found):
            continue
        if name in unmatched:
            continue
        matches = sibling_defs.get(name, [])
        if len(matches) == 1:
            sp = rel(root, matches[0])
            if sp in found_paths:
                continue
            found_paths.add(sp)
            found.append(
                {
                    "canonical_name": name,
                    "source_path": sp,
                }
            )
        elif len(matches) > 1:
            ambiguous.append(
                {
                    "reference": name,
                    "candidates": [rel(root, c) for c in matches],
                }
            )
        else:
            unmatched.append(name)
    return found, unmatched, ambiguous


def collect_child_session_ids(exported: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    def add_id(value: Any) -> None:
        if isinstance(value, str) and SESSION_ID_RE.fullmatch(value):
            ids.add(value)

    for raw_msg in exported.get("messages", []):
        if not isinstance(raw_msg, dict):
            continue
        parts = raw_msg.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            tool = str(state.get("tool") or part.get("tool") or "").lower()
            if tool != "task":
                continue
            meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            add_id(meta.get("sessionId") or meta.get("sessionID"))
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            add_id(inp.get("task_id"))
            output = state.get("output")
            if isinstance(output, str):
                for m in TASK_ID_OUTPUT_RE.finditer(output):
                    add_id(m.group(1))

    for raw_msg in exported.get("messages", []):
        if not isinstance(raw_msg, dict):
            continue
        parts = raw_msg.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            if part.get("synthetic") is not True:
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            for m in TASK_ID_OUTPUT_RE.finditer(text):
                candidate = m.group(1)
                if candidate in ids:
                    add_id(candidate)
    return ids


def collect_unresolved_tasks(exported: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for raw_msg in exported.get("messages", []):
        if not isinstance(raw_msg, dict):
            continue
        parts = raw_msg.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            tool = str(state.get("tool") or part.get("tool") or "").lower()
            if tool != "task":
                continue
            status = str(state.get("status") or "unknown").lower()
            if status not in {"error", "failed", "failure"}:
                continue
            meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            sid = meta.get("sessionId") or meta.get("sessionID")
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            task_id = inp.get("task_id")
            output = state.get("output")
            has_id = (
                (isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid))
                or (isinstance(task_id, str) and SESSION_ID_RE.fullmatch(task_id))
                or (isinstance(output, str) and TASK_ID_OUTPUT_RE.search(output))
            )
            if not has_id:
                unresolved.append(
                    {
                        "parent_session_id": session_id,
                        "status": status,
                        "tool": tool,
                    }
                )
    return unresolved


def find_existing_export(session_dir: Path, session_id: str) -> list[Path]:
    if not session_dir.is_dir():
        return []
    matches: list[Path] = []
    for p in sorted(session_dir.glob("*.json")):
        try:
            data = load_json(p)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        if info.get("id") == session_id:
            matches.append(p)
    return matches


def export_session_via_cli(session_id: str, *, cwd: Path, timeout: int = 120) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id}")
    fd, tmp_name = tempfile.mkstemp(prefix="session-export-", suffix=".json")
    target = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            proc = subprocess.run(
                ["opencode", "--pure", "export", session_id],
                stdout=fh,
                stderr=subprocess.PIPE,
                timeout=timeout,
                cwd=str(cwd),
            )
    except FileNotFoundError as exc:
        target.unlink(missing_ok=True)
        raise ValueError("opencode CLI not found on PATH; cannot auto-export child session") from exc
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise ValueError(f"opencode export {session_id} timed out after {timeout}s") from exc
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"opencode export {session_id} failed (session must exist in this machine's storage): "
            f"{stderr.strip()[:500]}"
        )
    try:
        with target.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        target.unlink(missing_ok=True)
        raise ValueError(f"opencode export {session_id} did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        target.unlink(missing_ok=True)
        raise ValueError(f"opencode export {session_id} did not return a JSON object")
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    if info.get("id") != session_id:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"opencode export {session_id} returned mismatched session id: {info.get('id')!r}"
        )
    return target


def ensure_child_export(
    root: Path,
    session_dir: Path,
    session_id: str,
    *,
    missing: list[str],
    ambiguous: list[dict[str, Any]],
    parent_id: str | None,
    cwd: Path,
) -> Path | None:
    existing = find_existing_export(session_dir, session_id)
    if len(existing) > 1:
        ambiguous.append(
            {
                "session_id": session_id,
                "parent_session_id": parent_id,
                "candidates": [rel(root, m) for m in existing],
            }
        )
        return None
    if len(existing) == 1:
        data = load_json(existing[0])
        info = data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else {}
        parent = info.get("parentID") or info.get("parentId")
        if parent_id is not None and parent is not None and parent != parent_id:
            ambiguous.append({
                "session_id": session_id,
                "parent_session_id": parent_id,
                "candidates": [rel(root, existing[0])],
                "reason": "existing export has mismatched parent session id",
            })
            return None
        return existing[0]
    try:
        tmp = export_session_via_cli(session_id, cwd=cwd)
    except ValueError as exc:
        missing.append(session_id)
        return None
    try:
        data = load_json(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        missing.append(session_id)
        return None
    if not isinstance(data, dict):
        tmp.unlink(missing_ok=True)
        missing.append(session_id)
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    if info.get("id") != session_id:
        tmp.unlink(missing_ok=True)
        missing.append(session_id)
        return None
    parent = info.get("parentID") or info.get("parentId")
    if parent_id is not None and parent is not None and parent != parent_id:
        tmp.unlink(missing_ok=True)
        missing.append(session_id)
        return None
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / f"{session_id}.json"
    if target.exists():
        try:
            existing_data = load_json(target)
        except Exception:
            existing_data = None
        existing_id = None
        if isinstance(existing_data, dict):
            existing_info = existing_data.get("info") if isinstance(existing_data.get("info"), dict) else {}
            existing_id = existing_info.get("id")
        if existing_id != session_id:
            tmp.unlink(missing_ok=True)
            ambiguous.append(
                {
                    "session_id": session_id,
                    "parent_session_id": parent_id,
                    "candidates": [rel(root, target)],
                    "reason": "existing file has mismatched session id",
                }
            )
            return None
        tmp.unlink(missing_ok=True)
        return target
    os.replace(tmp, target)
    return target


def build_session_tree(
    root: Path,
    source_path: Path,
    explicit_child_paths: list[Path],
    cli_cwd: Path,
    session_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    seen: set[str] = set()
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    explicit_by_id: dict[str, list[Path]] = {}
    for p in explicit_child_paths:
        try:
            data = load_json(p)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        sid = info.get("id")
        if isinstance(sid, str) and sid.startswith("ses_"):
            explicit_by_id.setdefault(sid, []).append(p)

    def visit(path: Path, parent_id: str | None) -> None:
        try:
            data = load_json(path)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        sid = info.get("id")
        if not isinstance(sid, str) or not sid.startswith("ses_"):
            return
        if sid in seen:
            if parent_id is not None:
                edges.append(
                    {
                        "parent_session_id": parent_id,
                        "child_session_id": sid,
                        "kind": "cycle_or_duplicate",
                    }
                )
            return
        seen.add(sid)
        child_ids = sorted(collect_child_session_ids(data))
        record: dict[str, Any] = {
            "session_id": sid,
            "source_path": rel(root, path),
            "sha256": sha256_file(path),
            "parent_session_id": parent_id,
            "child_session_ids": child_ids,
        }
        records.append(record)
        if parent_id is not None:
            edges.append(
                {
                    "parent_session_id": parent_id,
                    "child_session_id": sid,
                    "kind": "parent_child",
                }
            )
        unresolved.extend(collect_unresolved_tasks(data, sid))
        for child_id in child_ids:
            if child_id in seen:
                edges.append(
                    {
                        "parent_session_id": sid,
                        "child_session_id": child_id,
                        "kind": "cycle_or_duplicate",
                    }
                )
                continue
            if child_id in explicit_by_id:
                matches = explicit_by_id[child_id]
                if len(matches) > 1:
                    ambiguous.append(
                        {
                            "session_id": child_id,
                            "parent_session_id": sid,
                            "candidates": [rel(root, m) for m in matches],
                        }
                    )
                    continue
                visit(matches[0], sid)
                continue
            child_path = ensure_child_export(
                root,
                session_dir,
                child_id,
                missing=missing,
                ambiguous=ambiguous,
                parent_id=sid,
                cwd=cli_cwd,
            )
            if child_path is not None:
                visit(child_path, sid)

    visit(source_path, None)
    return records, missing, ambiguous, edges, unresolved


def resolve_root_source(root: Path, raw: str) -> Path:
    path = safe_project_path(root, raw)
    if path.is_file():
        return path
    if not path.is_dir():
        raise ValueError(f"session_path is neither a file nor a directory: {raw}")
    json_files = sorted(p for p in path.glob("*.json") if p.is_file())
    md_files = sorted(p for p in path.glob("*.md") if p.is_file())

    def session_info_of(p: Path) -> dict[str, Any] | None:
        try:
            data = load_json(p)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        sid = info.get("id")
        if not isinstance(sid, str) or not sid.startswith("ses_"):
            return None
        return info

    valid_json = [(p, info) for p in json_files if (info := session_info_of(p)) is not None]
    if valid_json:
        by_id: dict[str, list[Path]] = {}
        for p, info in valid_json:
            by_id.setdefault(info["id"], []).append(p)
        dupes = {sid: paths for sid, paths in by_id.items() if len(paths) > 1}
        if dupes:
            raise RootSelectionError(
                "session directory has duplicate JSON exports for the same session id; "
                "cannot pick a root automatically",
                [rel(root, p) for paths in dupes.values() for p in paths],
            )

        child_ids: set[str] = set()
        for p, info in valid_json:
            try:
                data = load_json(p)
            except Exception:
                data = None
            if isinstance(data, dict):
                child_ids |= collect_child_session_ids(data)
            parent = info.get("parentID") or info.get("parentId")
            if isinstance(parent, str) and SESSION_ID_RE.fullmatch(parent):
                child_ids.add(info["id"])

        root_candidates = [
            (p, info)
            for p, info in valid_json
            if info["id"] not in child_ids
        ]
        if len(root_candidates) == 1:
            return root_candidates[0][0]
        if len(root_candidates) > 1:
            raise RootSelectionError(
                "session directory has multiple JSON exports that are not children "
                "of any other session; cannot pick a root automatically",
                [rel(root, p) for p, _ in root_candidates],
            )
        raise RootSelectionError(
            "session directory has no JSON export that is not a child of another "
            "session; cannot pick a root automatically",
            [rel(root, p) for p, _ in valid_json],
        )
    if len(md_files) == 1:
        return md_files[0]
    if md_files:
        raise RootSelectionError(
            "session directory has no JSON export and multiple .md files; "
            "cannot pick a root automatically",
            [rel(root, p) for p in md_files],
        )
    raise ValueError(f"session directory contains no usable .json or .md files: {raw}")


def main() -> None:
    run_dir: Path | None = None
    created_run = False
    try:
        from common import read_input

        data = read_input()
        root = project_root(data)
        source = resolve_root_source(root, str(data.get("session_path", "")))
        if source.is_dir():
            raise ValueError(
                f"session_path resolved to a directory, not a file: {source}"
            )
        original_source = source
        original_source_hash = sha256_file(source)
        rescued_session_id: str | None = None
        if source.suffix.lower() == ".md":
            source, rescued_session_id = rescue_markdown_export(source, root)
        chunk_chars = int(data.get("chunk_chars") or 5000)
        if chunk_chars < 500 or chunk_chars > 200_000:
            raise ValueError("chunk_chars must be between 500 and 200000")

        try:
            exported = load_json(source)
        except json.JSONDecodeError as exc:
            hint = ""
            if source.suffix.lower() == ".md":
                hint = " (Markdown export detected; re-export as JSON: opencode export <session-id> > sessions/raw/<file>.json)"
            raise ValueError(f"session export is not valid JSON: {exc}{hint}") from exc
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

        run_dir.mkdir(parents=True, exist_ok=False)
        created_run = True
        for sub in ["raw/prompts", "normalized", "chunks", "summaries", "derived", "reports"]:
            (run_dir / sub).mkdir(parents=True, exist_ok=True)

        raw_session = run_dir / "raw" / "session-export.json"
        shutil.copy2(source, raw_session)

        explicit_child_paths: list[Path] = []
        for item in data.get("child_export_paths") or []:
            if isinstance(item, str) and item.strip():
                explicit_child_paths.append(safe_project_path(root, item))
        cli_cwd = root
        session_dir = original_source.parent
        session_records, missing_child_exports, ambiguous_child_exports, session_edges, unresolved_tasks = build_session_tree(
            root, raw_session, explicit_child_paths, cli_cwd, session_dir
        )

        child_session_copies: list[dict[str, Any]] = []
        for rec in session_records:
            if rec["session_id"] == (exported.get("info") or {}).get("id"):
                continue
            src = root / rec["source_path"]
            dst = run_dir / "raw" / "child-sessions" / f"{rec['session_id']}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            child_session_copies.append(
                {
                    "session_id": rec["session_id"],
                    "parent_session_id": rec["parent_session_id"],
                    "source_path": rec["source_path"],
                    "run_path": rel(root, dst),
                    "sha256": rec["sha256"],
                }
            )

        prompt_records: list[dict[str, Any]] = []
        prompt_inputs: list[tuple[str, Path]] = []
        orchestrator_prompt = data.get("orchestrator_prompt")
        orchestrator_path: Path | None = None
        if isinstance(orchestrator_prompt, str) and orchestrator_prompt.strip():
            orch, dir_subagents = resolve_orchestrator(root, orchestrator_prompt)
            orchestrator_path = orch
            prompt_inputs.append(("orchestrator", orch))
            for sub_path in dir_subagents:
                prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))
        for item in data.get("subagent_prompts") or []:
            if isinstance(item, str) and item.strip():
                for sub_path in resolve_subagent(root, item):
                    prompt_inputs.append((f"subagent-{len(prompt_inputs):02d}", sub_path))

        missing_agent_prompts: list[str] = []
        ambiguous_agent_prompts: list[dict[str, Any]] = []
        referenced_agent_names: dict[str, str] = {}
        if orchestrator_path is not None:
            ref_agents, missing_agent_prompts, ambiguous_agent_prompts = resolve_referenced_agents(
                root, orchestrator_path
            )
            for ref_agent in ref_agents:
                sub_path = root / ref_agent["source_path"]
                role = f"subagent-{len(prompt_inputs):02d}"
                prompt_inputs.append((role, sub_path))
                referenced_agent_names[role] = ref_agent["canonical_name"]

        seen_prompt_paths: set[Path] = set()
        deduped_inputs: list[tuple[str, Path]] = []
        for role, src in prompt_inputs:
            resolved = src.resolve()
            if resolved in seen_prompt_paths:
                continue
            seen_prompt_paths.add(resolved)
            deduped_inputs.append((role, src))

        if missing_agent_prompts and orchestrator_path is not None:
            supplied = {p.resolve() for _, p in deduped_inputs}
            kept_missing: list[str] = []
            for ref in missing_agent_prompts:
                resolved = (orchestrator_path.parent / Path(ref).name).resolve()
                if resolved in supplied:
                    continue
                kept_missing.append(ref)
            missing_agent_prompts = kept_missing

        for role, src in deduped_inputs:
            src = src.resolve()
            suffix = src.suffix if src.suffix else ".md"
            dst = run_dir / "raw" / "prompts" / f"{role}-{slugify(src.stem, fallback='prompt')}{suffix}"
            shutil.copy2(src, dst)
            record: dict[str, Any] = {
                "role": role,
                "source_path": rel(root, src),
                "run_path": rel(root, dst),
                "sha256": sha256_file(dst),
                "canonical_name": read_frontmatter(src).get("name") or referenced_agent_names.get(role) or src.stem,
            }
            prompt_records.append(record)

        def normalize_transcript(
            transcript: dict[str, Any],
            *,
            prefix: str,
            session_id: str,
        ) -> dict[str, Any]:
            t_messages = transcript.get("messages")
            t_messages = [m for m in t_messages if isinstance(m, dict)] if isinstance(t_messages, list) else []
            t_blocks: list[dict[str, Any]] = []
            t_events: list[dict[str, Any]] = []
            t_timeline: list[dict[str, Any]] = []
            t_event_seq = 0
            t_message_records: list[dict[str, Any]] = []
            t_totals = {"cost": 0.0, "tokens": {"input": 0.0, "output": 0.0, "reasoning": 0.0, "cache_read": 0.0, "cache_write": 0.0}}

            def add_event(event: dict[str, Any]) -> None:
                nonlocal t_event_seq
                t_event_seq += 1
                if "event_id" not in event:
                    event["event_id"] = f"{prefix}e{t_event_seq:05d}"
                t_events.append(event)
                t_timeline.append({"type": "event", "ref": event["event_id"]})

            def add_block(kind: str, msg_id: str, content: str, source_refs: list[str], metadata: dict[str, Any]) -> None:
                if not content.strip():
                    return
                ordinal = len(t_blocks) + 1
                block_id = f"{prefix}b{ordinal:05d}-{kind}"
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
                    "content": content,
                }
                t_blocks.append(record)
                write_json(
                    chunk_path,
                    {
                        **record,
                        "content": content,
                    },
                )
                t_timeline.append({"type": "block", "ref": block_id})

            for msg_index, raw_msg in enumerate(t_messages, start=1):
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
                t_message_records.append(msg_meta)

                if isinstance(info.get("cost"), (int, float)):
                    t_totals["cost"] += float(info["cost"])
                tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
                for key in ["input", "output", "reasoning"]:
                    if isinstance(tokens.get(key), (int, float)):
                        t_totals["tokens"][key] += float(tokens[key])
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                if isinstance(cache.get("read"), (int, float)):
                    t_totals["tokens"]["cache_read"] += float(cache["read"])
                if isinstance(cache.get("write"), (int, float)):
                    t_totals["tokens"]["cache_write"] += float(cache["write"])

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
                        t_event_seq += 1
                        event = tool_event(part, msg_id, t_event_seq, prefix=prefix)
                        t_events.append(event)
                        t_timeline.append({"type": "event", "ref": event["event_id"]})
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

            pending_blocks = [
                {
                    "chunk_id": b["chunk_id"],
                    "kind": b["kind"],
                    "chunk_path": b["chunk_path"],
                    "char_count": b["char_count"],
                }
                for b in t_blocks
                if b["needs_summary"]
            ]
            return {
                "session_id": session_id,
                "session_info": transcript.get("info"),
                "events": t_events,
                "timeline": t_timeline,
                "totals": t_totals,
                "messages": t_message_records,
                "blocks": t_blocks,
                "pending": pending_blocks,
            }

        root_session_id = (exported.get("info") or {}).get("id") or "root"
        root_view = normalize_transcript(exported, prefix="", session_id=root_session_id)

        child_views: list[dict[str, Any]] = []
        for rec in session_records:
            if rec["session_id"] == root_session_id:
                continue
            child_path = root / rec["source_path"]
            try:
                child_exported = load_json(child_path)
            except Exception:
                continue
            if not isinstance(child_exported, dict):
                continue
            child_views.append(
                normalize_transcript(
                    child_exported,
                    prefix=f"{rec['session_id']}-",
                    session_id=rec["session_id"],
                )
            )

        sessions_views = [root_view] + child_views
        all_blocks = [b for v in sessions_views for b in v["blocks"]]
        all_events = [e for v in sessions_views for e in v["events"]]
        all_timeline = [t for v in sessions_views for t in v["timeline"]]
        all_messages = [m for v in sessions_views for m in v["messages"]]
        all_totals = {
            "cost": sum(v["totals"]["cost"] for v in sessions_views),
            "tokens": {
                "input": sum(v["totals"]["tokens"]["input"] for v in sessions_views),
                "output": sum(v["totals"]["tokens"]["output"] for v in sessions_views),
                "reasoning": sum(v["totals"]["tokens"]["reasoning"] for v in sessions_views),
                "cache_read": sum(v["totals"]["tokens"]["cache_read"] for v in sessions_views),
                "cache_write": sum(v["totals"]["tokens"]["cache_write"] for v in sessions_views),
            },
        }

        normalized = {
            "session_info": exported.get("info"),
            "messages": all_messages,
            "events": all_events,
            "timeline": all_timeline,
            "totals": all_totals,
            "source_sha256": session_hash,
            "sessions": [
                {
                    "session_id": v["session_id"],
                    "session_info": v["session_info"],
                    "events": v["events"],
                    "timeline": v["timeline"],
                    "totals": v["totals"],
                    "messages": v["messages"],
                    "blocks": v["blocks"],
                }
                for v in sessions_views
            ],
        }
        write_json(run_dir / "normalized" / "session.json", normalized)
        write_json(run_dir / "normalized" / "events.json", all_events)

        pending = [p for v in sessions_views for p in v["pending"]]

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
                "original_path": rel(root, original_source),
                "original_sha256": original_source_hash,
                "run_copy": rel(root, raw_session),
                "sha256": session_hash,
                "rescued_from_markdown": rescued_session_id,
            },
            "prompts": prompt_records,
            "config": {"chunk_chars": chunk_chars},
            "counts": {
                "messages": len(all_messages),
                "events": len(all_events),
                "blocks": len(all_blocks),
                "pending_summaries": len(pending),
            },
            "blocks": all_blocks,
            "session_tree": {
                "root_session_id": (exported.get("info") or {}).get("id"),
                "sessions": session_records,
                "child_session_copies": child_session_copies,
                "missing_agent_prompts": missing_agent_prompts,
                "ambiguous_agent_prompts": ambiguous_agent_prompts,
                "missing_child_exports": missing_child_exports,
                "ambiguous_child_exports": ambiguous_child_exports,
                "edges": session_edges,
                "unresolved_tasks": unresolved_tasks,
            },
        }
        write_json(run_dir / "manifest.json", manifest)

        warnings = []
        if not messages:
            warnings.append("session contains no messages")
        if rescued_session_id:
            warnings.append(
                f"markdown input re-exported as JSON via opencode CLI (session {rescued_session_id})"
            )
        if missing_agent_prompts:
            warnings.append(
                "unmatched agent prompt references (reported for controller question): "
                + ", ".join(missing_agent_prompts)
            )
        if missing_child_exports:
            warnings.append(
                "missing child session exports (reported for controller question): "
                + ", ".join(missing_child_exports)
            )
        emit(
            {
                "ok": True,
                "run_id": run_id,
                "manifest_path": rel(root, run_dir / "manifest.json"),
                "counts": manifest["counts"],
                "pending_summaries": pending,
                "prompt_paths": [p["run_path"] for p in prompt_records],
                "session_tree": manifest["session_tree"],
                "missing_agent_prompts": missing_agent_prompts,
                "ambiguous_agent_prompts": ambiguous_agent_prompts,
                "missing_child_exports": missing_child_exports,
                "ambiguous_child_exports": ambiguous_child_exports,
                "warnings": warnings,
            }
        )
    except RootSelectionError as exc:
        if created_run and run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        emit(
            {
                "ok": False,
                "needs_input": True,
                "error": exc.message,
                "candidates": exc.candidates,
            }
        )
        raise SystemExit(2)
    except Exception as exc:
        if created_run and run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        fail(str(exc))


if __name__ == "__main__":
    main()
