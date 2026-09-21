from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CHUNK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("empty JSON input")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool input must be a JSON object")
    return value


def emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fail(message: str, *, details: Any | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details is not None:
        payload["details"] = details
    emit(payload)
    raise SystemExit(2)


def project_root(data: dict[str, Any]) -> Path:
    root = Path(str(data.get("project_root", ""))).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"invalid project_root: {root}")
    return root


def safe_project_path(root: Path, raw: str, *, must_exist: bool = True) -> Path:
    if not raw or not str(raw).strip():
        raise ValueError("path is empty")
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must stay inside project root: {raw}") from exc
    if must_exist and not resolved.exists():
        raise ValueError(f"path does not exist: {raw}")
    return resolved


def safe_run_dir(root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise ValueError("invalid run_id")
    run_dir = (root / "runs" / run_id).resolve()
    try:
        run_dir.relative_to((root / "runs").resolve())
    except ValueError as exc:
        raise ValueError("run directory escaped runs/") from exc
    if not run_dir.is_dir():
        raise ValueError(f"run not found: {run_id}")
    return run_dir


def safe_chunk_id(chunk_id: str) -> str:
    if not CHUNK_ID_RE.fullmatch(chunk_id or ""):
        raise ValueError("invalid chunk_id")
    return chunk_id


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        if text and not text.endswith("\n"):
            handle.write("\n")
    os.replace(tmp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str, *, fallback: str = "run", limit: int = 48) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return (value or fallback)[:limit]


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_preview(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
