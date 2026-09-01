"""Fail-safe P9b dotfiles hygiene.

The OMP invocation in this module is deliberately classification-only.  It may
read bounded interactive-session context and exact diffs, but it never receives
permission to mutate the checkout.  Python owns the final race checks, exact
path staging, commit, push, and independent remote verification.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import HOME, SECRET_PATTERNS, SMALL_MODEL
from .runtime import _call_omp_p, _extract_json, atomic_write_json, run, run_capture_ok

DOTFILES_GIT = HOME / ".dotfiles-homelab"
SESSION_ROOT = HOME / ".omp" / "agent" / "sessions"
ACTIVE_SESSION_MINUTES = 12 * 60
MAX_TITLE_CHARS = 160
MAX_CWD_CHARS = 512
MAX_CONTEXT_CHARS = 2400
MAX_DIFF_CHARS = 200_000
MAX_TOTAL_DIFF_CHARS = 800_000
CLASSIFIER_ISOLATION_ARGS = (
    "--no-tools",
    "--no-extensions",
    "--no-skills",
    "--no-rules",
    "--no-session",
    "--no-title",
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?:api[_-]?key|secret(?:[_-]?access)?[_-]?key|password|passwd|auth[_-]?token|
       access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key)
    [\"']?\s*[:=]\s*(?P<value>.+)$
    """
)
CLASSIFICATIONS = frozenset({
    "unrelated", "active", "ambiguous", "sensitive", "out_of_scope",
})

# These are the only work-tree roots P9b may ever consider.  The path check is
# component-aware; a prefix such as ``~/.config.evil`` is not accepted.
def allowed_prefixes(home: Path | None = None) -> tuple[Path, ...]:
    root = Path(home if home is not None else HOME)
    return tuple(
        root / part
        for part in (
            ".config",
            ".local/bin",
            ".zshrc",
            ".omp",
            "scripts",
            "open-webui",
            "searxng",
            "k3s",
            ".config/systemd/user",
        )
    )

def _bounded(value: object, limit: int) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    return text[:limit]


def _normalise_status_path(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            value = value[1:-1]
    return value.strip().strip("\"'")


def parse_status_paths(status: str | bytes) -> list[str]:
    """Parse porcelain status into unique repository-relative paths.

    Both line-delimited ``--short`` output and NUL-delimited porcelain output
    are accepted.  Renames contribute both sides so neither exact path can be
    accidentally left out of a commit or race check.
    """
    if isinstance(status, bytes):
        status = status.decode("utf-8", "replace")
    nul_delimited = "\x00" in status
    lines = status.split("\x00") if nul_delimited else status.splitlines()
    paths: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.isspace():
            continue
        if nul_delimited and (len(line) < 3 or not line[2].isspace()):
            payload = line
        else:
            payload = line[3:] if len(line) >= 3 else line[2:]
        payload = payload.strip()
        rename_parts = re.split(r"\s+->\s+", payload, maxsplit=1)
        candidates = rename_parts if len(rename_parts) == 2 else [payload]
        for candidate in candidates:
            path = _normalise_status_path(candidate)
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _status_map(status: str | bytes) -> dict[str, str]:
    """Return exact path -> two-character porcelain status."""
    if isinstance(status, bytes):
        status = status.decode("utf-8", "replace")
    result: dict[str, str] = {}
    if "\x00" in status:
        items = status.split("\x00")
        index = 0
        while index < len(items):
            line = items[index]
            index += 1
            if not line or len(line) < 3:
                continue
            code = line[:2]
            path = _normalise_status_path(line[3:])
            if path:
                result[path] = code
            if ("R" in code or "C" in code) and index < len(items):
                source = _normalise_status_path(items[index])
                index += 1
                if source:
                    result[source] = code
        return result
    for line in status.splitlines():
        if not line or len(line) < 3:
            continue
        code = line[:2]
        parts = re.split(r"\s+->\s+", line[3:].strip(), maxsplit=1)
        for part in parts:
            path = _normalise_status_path(part)
            if path:
                result[path] = code
    return result


def _safe_home_path(path: str, home: Path | None = None) -> Path | None:
    """Resolve a relative work-tree path without permitting traversal."""
    candidate = Path(path)
    if candidate.is_absolute() or "\x00" in path:
        return None
    root = Path(home if home is not None else HOME).resolve()
    try:
        resolved = (root / candidate).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved

def _path_in_allowed_scope(path: str, home: Path | None = None) -> bool:
    resolved = _safe_home_path(path, home)
    if resolved is None:
        return False
    for prefix in allowed_prefixes(home):
        try:
            resolved.relative_to(prefix.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False
def _is_sensitive(path: str) -> bool:
    candidate = Path(path)
    values = (path, candidate.name, *candidate.parts)
    return any(
        pattern.match(value.lower())
        for pattern in SECRET_PATTERNS
        for value in values
    )


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)


def _session_evidence(path: Path, now: datetime, active_window: timedelta) -> dict[str, Any] | None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    if now - mtime > active_window:
        return None

    malformed = False
    closed = False
    title = ""
    cwd = ""
    messages: list[str] = []
    session_id = path.stem
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    malformed = True
                    continue
                if not isinstance(item, Mapping):
                    malformed = True
                    continue
                if item.get("customType") == "session_exit":
                    closed = True
                if item.get("type") == "session":
                    session_id = _bounded(item.get("id") or session_id, 160)
                    title = _bounded(item.get("title") or title, MAX_TITLE_CHARS)
                    cwd = _bounded(item.get("cwd") or cwd, MAX_CWD_CHARS)
                elif item.get("type") == "title":
                    title = _bounded(item.get("title") or title, MAX_TITLE_CHARS)
                message = item.get("message")
                if isinstance(message, Mapping):
                    role = message.get("role")
                    if role in {"user", "assistant"}:
                        text = _message_text(message).strip()
                        if text:
                            messages.append(f"{role}: {text}")
                    cwd = _bounded(message.get("cwd") or cwd, MAX_CWD_CHARS)
                cwd = _bounded(item.get("cwd") or cwd, MAX_CWD_CHARS)
    except OSError:
        malformed = True

    if closed:
        return None
    context = "\n".join(messages[-8:])
    return {
        "path": str(path),
        "session_id": session_id,
        "title": _bounded(title or path.stem, MAX_TITLE_CHARS),
        "cwd": _bounded(cwd, MAX_CWD_CHARS),
        "recent_context": _bounded(context, MAX_CONTEXT_CHARS),
        "mtime": mtime.isoformat(),
        "malformed": malformed,
        "closed": False,
    }


def collect_active_session_evidence(
    session_root: Path | None = None,
    *,
    now: datetime | None = None,
    active_window_minutes: int = ACTIVE_SESSION_MINUTES,
) -> list[dict[str, Any]]:
    """Collect recursively discovered, recent, unclosed interactive sessions.

    A malformed recent transcript is retained as evidence rather than ignored:
    the caller must fail closed and classify affected paths as ambiguous.
    """
    root = Path(session_root if session_root is not None else HOME / ".omp" / "agent" / "sessions")
    if not root.is_dir():
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=max(0, int(active_window_minutes)))
    evidence: list[dict[str, Any]] = []
    try:
        paths = sorted(root.rglob("*.jsonl"))
    except OSError:
        return [{
            "path": str(root), "session_id": "", "title": "", "cwd": "",
            "recent_context": "", "mtime": "", "malformed": True, "closed": False,
        }]
    for path in paths:
        if not path.is_file():
            continue
        item = _session_evidence(path, current, window)
        if item is not None:
            evidence.append(item)
    return evidence


def _session_mentions_path(session: Mapping[str, Any], path: str, home: Path) -> bool:
    relative = Path(path)
    absolute = _safe_home_path(path, home)
    home_root = home.resolve()
    cwd_value = str(session.get("cwd") or "")
    if cwd_value:
        try:
            cwd = Path(cwd_value).expanduser().resolve(strict=False)
            # A session launched at $HOME is not evidence that every file in
            # the homedir is active. Specific project/subdirectory CWDs are.
            if cwd != home_root and absolute is not None and (absolute == cwd or cwd in absolute.parents):
                return True
        except (OSError, RuntimeError):
            return True
    context = str(session.get("recent_context") or "")
    normalized = str(relative).replace("\\", "/")
    if normalized and normalized in context.replace("\\", "/"):
        return True
    if absolute is not None and str(absolute) in context:
        return True
    return False


def exact_path_diff(path: str, *, git_dir: Path = DOTFILES_GIT, home: Path = HOME) -> str:
    """Return the complete staged-form diff for exactly one work-tree path."""
    if _safe_home_path(path, home) is None:
        raise ValueError(f"unsafe dotfiles path: {path}")
    fd, index_path = tempfile.mkstemp(prefix="steward-dotfiles-index-")
    os.close(fd)
    os.unlink(index_path)
    env = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        _, stderr, code = run_capture_ok(
            _git_command(git_dir, home, "read-tree", "HEAD"),
            env=env,
        )
        if code != 0:
            raise RuntimeError(stderr or "could not initialize temporary index")
        _, stderr, code = run_capture_ok(
            _git_command(git_dir, home, "add", "-A", "--", path),
            env=env,
        )
        if code != 0:
            raise RuntimeError(stderr or f"could not stage {path} in temporary index")
        stdout, stderr, code = run_capture_ok(
            _git_command(
                git_dir,
                home,
                "diff",
                "--cached",
                "--no-renames",
                "--full-index",
                "--binary",
                "--no-ext-diff",
                "--no-color",
                "HEAD",
                "--",
                path,
            ),
            env=env,
        )
        if code != 0:
            raise RuntimeError(stderr or f"could not diff {path}")
        return stdout
    finally:
        try:
            os.remove(index_path)
        except FileNotFoundError:
            pass


def _cached_path_diff(path: str, *, git_dir: Path, home: Path) -> str:
    stdout, stderr, code = run_capture_ok(
        _git_command(
            git_dir,
            home,
            "diff",
            "--cached",
            "--no-renames",
            "--full-index",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "HEAD",
            "--",
            path,
        )
    )
    if code != 0:
        raise RuntimeError(stderr or f"could not read staged diff for {path}")
    return stdout


def _diff_hash(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8", "surrogatepass")).hexdigest()


def _sensitive_diff_reason(diff: str) -> str | None:
    if "GIT binary patch" in diff or "\nBinary files " in diff:
        return "binary changes require manual review"
    if len(diff) > MAX_DIFF_CHARS:
        return f"diff exceeds {MAX_DIFF_CHARS} characters"
    for line in diff.splitlines():
        if not line or line[0] not in {"+", "-"} or line.startswith(("+++", "---")):
            continue
        text = line[1:].strip()
        if "BEGIN " in text and "PRIVATE KEY" in text:
            return "diff contains private-key material"
        match = _SECRET_ASSIGNMENT.search(text)
        if not match:
            continue
        value = match.group("value").strip().rstrip(",").strip("\"'")
        safe_markers = (
            "${", "os.environ", "getenv(", "secret_ref", "<redacted>",
            "changeme", "example", "placeholder", "none", "null",
        )
        if len(value) >= 8 and not any(marker in value.lower() for marker in safe_markers):
            return "diff contains a credential-like assignment"
    return None


def classify_dotfile_paths(
    changed_paths: Iterable[str],
    *,
    home: Path = HOME,
    active_sessions: Iterable[Mapping[str, Any]] = (),
) -> dict[str, dict[str, str]]:
    """Apply deterministic safety classifications before OMP is consulted."""
    sessions = list(active_sessions)
    result: dict[str, dict[str, str]] = {}
    malformed = any(bool(session.get("malformed")) for session in sessions)
    for path in dict.fromkeys(str(item) for item in changed_paths):
        if not _path_in_allowed_scope(path, home):
            result[path] = {"classification": "out_of_scope", "reason": "path is outside the allowlist"}
        elif _is_sensitive(path):
            result[path] = {"classification": "sensitive", "reason": "path matches the secret denylist"}
        elif malformed:
            result[path] = {"classification": "ambiguous", "reason": "recent session evidence is malformed"}
        elif any(_session_mentions_path(session, path, home) for session in sessions):
            result[path] = {"classification": "active", "reason": "path is in active session context"}
    return result


def validate_classification_packet(
    packet: Any,
    expected_paths: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Validate the exact OMP classification schema, rejecting ambiguity."""
    expected = list(dict.fromkeys(str(path) for path in expected_paths))
    if not isinstance(packet, Mapping) or set(packet) != {"classifications"}:
        raise ValueError("classification packet must contain only classifications")
    rows = packet.get("classifications")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("classification packet must contain one row per path")
    expected_set = set(expected)
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"path", "classification", "reason"}:
            raise ValueError("classification row has an invalid schema")
        path = row.get("path")
        classification = row.get("classification")
        reason = row.get("reason")
        if (
            not isinstance(path, str) or path not in expected_set
            or path in result or not isinstance(classification, str)
            or classification not in CLASSIFICATIONS or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 500
        ):
            raise ValueError("classification row is invalid or ambiguous")
        result[path] = {"classification": classification, "reason": reason.strip()}
    if set(result) != expected_set:
        raise ValueError("classification packet omitted or invented a path")
    return result


def _redact_context(value: object) -> str:
    redacted: list[str] = []
    for line in _bounded(value, MAX_CONTEXT_CHARS).splitlines():
        match = _SECRET_ASSIGNMENT.search(line)
        if match:
            line = line[:match.start("value")] + "[REDACTED]"
        if "BEGIN " in line and "PRIVATE KEY" in line:
            line = "[REDACTED PRIVATE KEY MATERIAL]"
        redacted.append(line)
    return "\n".join(redacted)


def build_classification_prompt(
    records: Iterable[Mapping[str, Any]],
    sessions: Iterable[Mapping[str, Any]],
) -> str:
    """Build a bounded-context, exact-diff classification-only prompt."""
    rows = list(records)
    session_rows = []
    for session in sessions:
        session_rows.append({
            "title": _bounded(session.get("title"), MAX_TITLE_CHARS),
            "cwd": _bounded(session.get("cwd"), MAX_CWD_CHARS),
            "recent_context": _redact_context(session.get("recent_context")),
            "malformed": bool(session.get("malformed")),
        })
    return (
        "You are a read-only dotfiles change classifier. Do not run git add, commit, "
        "push, or any mutation. Classify every supplied path exactly once. "
        "Use unrelated only when the complete diff is clearly unrelated to active "
        "interactive work. Use active when it overlaps a session; ambiguous for "
        "uncertainty. Return ONLY this strict JSON object with no extra keys: "
        '{"classifications":[{"path":"...","classification":"unrelated|active|ambiguous|sensitive|out_of_scope",'
        '"reason":"..."}]}\n\n'
        f"ACTIVE SESSION CONTEXT (bounded):\n{json.dumps(session_rows, ensure_ascii=False)}\n\n"
        f"PATHS AND EXACT FULL TEXT DIFFS:\n{json.dumps(rows, ensure_ascii=False)}"
    )


def _git_command(git_dir: Path, home: Path, *args: str) -> list[str]:
    return ["/usr/bin/git", "--git-dir", str(git_dir), "--work-tree", str(home), *args]


def _snapshot(git_dir: Path, home: Path) -> tuple[dict[str, str], str | None]:
    stdout, stderr, code = run_capture_ok(
        _git_command(
            git_dir,
            home,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
    )
    if code != 0:
        return {}, stderr or stdout or "git status failed"
    return _status_map(stdout), None


def _staged_paths(git_dir: Path, home: Path) -> tuple[set[str], str | None]:
    stdout, stderr, code = run_capture_ok(
        _git_command(
            git_dir,
            home,
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "-z",
        )
    )
    if code != 0:
        return set(), stderr or stdout or "git staged diff failed"
    if "\x00" in stdout:
        paths = {
            _normalise_status_path(item)
            for item in stdout.split("\x00")
            if item
        }
        return {path for path in paths if path}, None
    return {
        _normalise_status_path(item)
        for item in stdout.splitlines()
        if _normalise_status_path(item)
    }, None


def _current_branch(git_dir: Path, home: Path) -> str | None:
    stdout, _, code = run_capture_ok(
        _git_command(git_dir, home, "symbolic-ref", "--short", "HEAD")
    )
    branch = stdout.strip()
    return branch if code == 0 and branch else None


def _head_oid(git_dir: Path, home: Path) -> str | None:
    stdout, _, code = run_capture_ok(
        _git_command(git_dir, home, "rev-parse", "HEAD")
    )
    value = stdout.strip()
    return value if code == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _remote_oid(git_dir: Path, home: Path, branch: str) -> str | None:
    stdout, _, code = run_capture_ok(
        _git_command(git_dir, home, "ls-remote", "origin", f"refs/heads/{branch}")
    )
    if code != 0:
        return None
    fields = stdout.split()
    return fields[0] if fields and re.fullmatch(r"[0-9a-f]{40}", fields[0]) else None


def _pending_push_record(branch: str, commit: str) -> dict[str, str]:
    """Return the exact immutable identity that may be retried later."""
    return {"branch": branch, "commit": commit}


def _retry_pending_push(
    pending: Mapping[str, Any],
    *,
    git_dir: Path,
    home: Path,
    paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Push only the exact committed branch/OID recorded by a prior attempt."""
    branch = pending.get("branch")
    commit = pending.get("commit")
    base = {
        "paths": list(paths),
        "commit": commit,
        "branch": branch,
        "pending_push": dict(pending),
    }
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or any(char.isspace() or char == "\x00" for char in branch)
        or not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        return {
            **base,
            "status": "pending_diverged",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": "pending push record is malformed",
        }

    current_branch = _current_branch(git_dir, home)
    current_head = _head_oid(git_dir, home)
    if current_branch != branch:
        return {
            **base,
            "status": "pending_diverged",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": (
                f"pending push branch diverged: expected {branch!r}, "
                f"found {current_branch!r}"
            ),
        }
    if current_head != commit:
        return {
            **base,
            "status": "pending_diverged",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": (
                f"pending push HEAD diverged: expected {commit}, "
                f"found {current_head or 'missing'}"
            ),
        }

    statuses, status_error = _snapshot(git_dir, home)
    staged, staged_error = _staged_paths(git_dir, home)
    if status_error or staged_error or statuses or staged:
        return {
            **base,
            "status": "pending_diverged",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": (
                status_error
                or staged_error
                or "working tree changed since pending commit"
            ),
        }

    # The original push may have reached the remote before reporting failure.
    remote = _remote_oid(git_dir, home, branch)
    if remote == commit:
        return {
            **base,
            "status": "committed",
            "push": "verified",
            "remote_commit": remote,
        } | {"pending_push": None}

    try:
        run(
            _git_command(
                git_dir,
                home,
                "push",
                "origin",
                f"{commit}:refs/heads/{branch}",
            ),
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return {
            **base,
            "status": "push_failed",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": str(exc),
        }
    verified = _remote_oid(git_dir, home, branch)
    if verified != commit:
        return {
            **base,
            "status": "remote_unverified",
            "phase_status": "failed",
            "phase_failed": True,
            "reason": "remote did not verify the exact pending commit",
            "remote_commit": verified,
        }
    return {
        **base,
        "status": "committed",
        "push": "verified",
        "remote_commit": verified,
        "pending_push": None,
    }

def _commit_exact_paths(
    paths: list[str],
    *,
    git_dir: Path,
    home: Path,
    expected_dirty_paths: Iterable[str] | None = None,
    expected_diff_hashes: Mapping[str, str] | None = None,
    push: bool = True,
    pre_push_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Commit only staged bytes identical to the diffs reviewed by the agent."""
    approved = set(paths)
    expected = set(expected_dirty_paths or paths)
    reviewed_hashes = dict(expected_diff_hashes or {})
    untouched = expected - approved
    if not approved:
        return {"status": "nothing_to_commit", "paths": []}

    def unstage_owned_paths() -> None:
        try:
            run(
                _git_command(git_dir, home, "reset", "--quiet", "HEAD", "--", *paths),
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, OSError):
            pass

    before, error = _snapshot(git_dir, home)
    if error:
        return {"status": "ambiguous", "reason": error}
    if set(before) != expected:
        return {"status": "ambiguous", "reason": "dirty paths changed before staging"}
    if any(code[0] not in {" ", "?"} for code in before.values()):
        return {
            "status": "ambiguous",
            "reason": "pre-staged index state is not owned by P9b",
        }
    try:
        run(
            _git_command(git_dir, home, "add", "-A", "--", *paths),
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return {"status": "ambiguous", "reason": f"exact-path staging failed: {exc}"}

    staged, error = _staged_paths(git_dir, home)
    if error or staged != approved:
        unstage_owned_paths()
        return {
            "status": "ambiguous",
            "reason": error or "staged paths differ from approved paths",
        }
    if reviewed_hashes:
        try:
            staged_hashes = {
                path: _diff_hash(
                    _cached_path_diff(path, git_dir=git_dir, home=home)
                )
                for path in paths
            }
        except RuntimeError as exc:
            unstage_owned_paths()
            return {"status": "ambiguous", "reason": str(exc)}
        expected_hashes = {path: reviewed_hashes.get(path, "") for path in paths}
        if staged_hashes != expected_hashes:
            unstage_owned_paths()
            return {
                "status": "ambiguous",
                "reason": "staged content differs from reviewed diff",
            }

    after_stage, error = _snapshot(git_dir, home)
    if error or set(after_stage) != expected:
        unstage_owned_paths()
        return {
            "status": "ambiguous",
            "reason": error or "dirty paths changed during staging",
        }
    try:
        run(
            _git_command(
                git_dir,
                home,
                "commit",
                "-m",
                "chore: steward dotfiles hygiene",
            ),
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        unstage_owned_paths()
        return {"status": "ambiguous", "reason": f"commit failed: {exc}"}

    local_oid = _head_oid(git_dir, home)
    branch = _current_branch(git_dir, home)
    if not local_oid or not branch:
        return {
            "status": "ambiguous",
            "reason": "could not identify committed branch",
        }
    result: dict[str, Any] = {
        "status": "committed",
        "paths": paths,
        "untouched_paths": sorted(untouched),
        "commit": local_oid,
        "branch": branch,
    }
    if not push:
        result["push"] = "not_requested"
        return result

    before_push, error = _snapshot(git_dir, home)
    staged_after, staged_error = _staged_paths(git_dir, home)
    if error or set(before_push) != untouched or staged_error or staged_after:
        return {
            "status": "push_pending",
            "commit": local_oid,
            "branch": branch,
            "pending_push": _pending_push_record(branch, local_oid),
            "reason": error or staged_error or "race detected before push",
        }
    if pre_push_check is not None and not pre_push_check():
        return {
            "status": "push_pending",
            "commit": local_oid,
            "branch": branch,
            "pending_push": _pending_push_record(branch, local_oid),
            "reason": "race detected before push",
        }
    try:
        run(
            _git_command(
                git_dir,
                home,
                "push",
                "origin",
                f"{local_oid}:refs/heads/{branch}",
            ),
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return {
            "status": "push_failed",
            "commit": local_oid,
            "branch": branch,
            "pending_push": _pending_push_record(branch, local_oid),
            "reason": str(exc),
        }
    verified = _remote_oid(git_dir, home, branch)
    if verified != local_oid:
        return {
            "status": "remote_unverified",
            "commit": local_oid,
            "branch": branch,
            "pending_push": _pending_push_record(branch, local_oid),
            "remote_commit": verified,
        }
    result.update({"push": "verified", "remote_commit": verified})
    return result


def phase_9b_dotfiles(
    run_dir: Path,
    dry_run: bool = False,
    *,
    git_dir: Path | None = None,
    home: Path | None = None,
    session_root: Path | None = None,
    push: bool = True,
    omp_call: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Classify and, only when proven safe, commit exact dotfiles paths."""
    print("[P9b] dotfiles hygiene")
    artifact = Path(run_dir) / "09b-dotfiles.json"
    root_home = Path(home if home is not None else HOME)
    root_git = Path(git_dir if git_dir is not None else root_home / ".dotfiles-homelab")
    root_sessions = Path(
        session_root
        if session_root is not None
        else root_home / ".omp" / "agent" / "sessions"
    )
    prior_artifact: dict[str, Any] = {}
    if artifact.is_file():
        try:
            loaded = json.loads(artifact.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prior_artifact = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            prior_artifact = {}
    pending = prior_artifact.get("pending_push")
    if (
        pending is None
        and prior_artifact.get("status") in {
            "push_pending", "push_failed", "remote_unverified"
        }
        and prior_artifact.get("branch") is not None
        and prior_artifact.get("commit") is not None
    ):
        # Migrate the pre-pending-push artifact shape without ever inventing
        # a new commit: only the recorded branch/OID may be retried.
        pending = {
            "branch": prior_artifact.get("branch"),
            "commit": prior_artifact.get("commit"),
        }
    if pending is not None:
        prior_paths = prior_artifact.get("paths", [])
        if not isinstance(prior_paths, list):
            prior_paths = []
        if isinstance(pending, Mapping):
            result = _retry_pending_push(
                pending,
                git_dir=root_git,
                home=root_home,
                paths=(str(path) for path in prior_paths),
            )
        else:
            result = {
                "status": "pending_diverged",
                "phase_status": "failed",
                "phase_failed": True,
                "reason": "pending push record is malformed",
                "pending_push": pending,
                "paths": prior_paths,
            }
        if result.get("pending_push") is None:
            result.pop("pending_push", None)
        atomic_write_json(artifact, result)
        print(f"[P9b] {result.get('status')} -> {artifact}")
        return result
    statuses, status_error = _snapshot(root_git, root_home)
    if status_error:
        result = {"status": "ambiguous", "reason": status_error, "changed_paths": []}
        atomic_write_json(artifact, result)
        return result
    changed = sorted(statuses)
    if not changed:
        result = {"status": "clean", "changed_paths": [], "classifications": {}}
        atomic_write_json(artifact, result)
        return result

    sessions = collect_active_session_evidence(root_sessions)
    session_fingerprint = _diff_hash(
        json.dumps(sessions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    deterministic = classify_dotfile_paths(
        changed, home=root_home, active_sessions=sessions
    )
    records: list[dict[str, Any]] = []
    reviewed_diff_hashes: dict[str, str] = {}
    reviewed_diff_sizes: dict[str, int] = {}
    total_diff_chars = 0
    for path in changed:
        if path in deterministic:
            continue
        try:
            diff = exact_path_diff(path, git_dir=root_git, home=root_home)
        except (OSError, RuntimeError, ValueError) as exc:
            deterministic[path] = {
                "classification": "ambiguous",
                "reason": f"exact diff unavailable: {str(exc)[:300]}",
            }
            continue
        if not diff:
            deterministic[path] = {
                "classification": "ambiguous",
                "reason": "exact diff is empty",
            }
            continue
        risk = _sensitive_diff_reason(diff)
        if risk:
            classification = (
                "sensitive"
                if "credential" in risk or "private-key" in risk
                else "ambiguous"
            )
            deterministic[path] = {
                "classification": classification,
                "reason": risk,
            }
            continue
        if total_diff_chars + len(diff) > MAX_TOTAL_DIFF_CHARS:
            deterministic[path] = {
                "classification": "ambiguous",
                "reason": f"aggregate diff exceeds {MAX_TOTAL_DIFF_CHARS} characters",
            }
            continue
        total_diff_chars += len(diff)
        reviewed_diff_hashes[path] = _diff_hash(diff)
        reviewed_diff_sizes[path] = len(diff)
        records.append({"path": path, "diff": diff})

    candidate_paths = [row["path"] for row in records]
    agent_packet: dict[str, dict[str, str]] | None = None
    agent_error = ""
    if candidate_paths:
        prompt = build_classification_prompt(records, sessions)
        call = omp_call or _call_omp_p
        try:
            raw = call(
                prompt,
                model=SMALL_MODEL,
                timeout=600,
                mode="json",
                extra_args=CLASSIFIER_ISOLATION_ARGS,
            )
            packet = _extract_json(raw, "dotfiles classification")
            agent_packet = validate_classification_packet(packet, candidate_paths)
        except Exception as exc:
            agent_error = str(exc)[:500]

    classifications = dict(deterministic)
    if agent_packet is not None:
        classifications.update(agent_packet)
    else:
        for path in candidate_paths:
            classifications[path] = {
                "classification": "ambiguous",
                "reason": agent_error or "classification unavailable",
            }

    eligible = sorted(
        path
        for path, row in classifications.items()
        if row.get("classification") == "unrelated"
    )
    session_artifacts = [
        {
            "session_id": session.get("session_id", ""),
            "title": _bounded(session.get("title"), MAX_TITLE_CHARS),
            "cwd": _bounded(session.get("cwd"), MAX_CWD_CHARS),
            "mtime": session.get("mtime", ""),
            "malformed": bool(session.get("malformed")),
        }
        for session in sessions
    ]
    base = {
        "changed_paths": changed,
        "active_sessions": session_artifacts,
        "active_sessions_sha256": session_fingerprint,
        "classifications": classifications,
        "reviewed_diffs": {
            path: {
                "sha256": reviewed_diff_hashes[path],
                "characters": reviewed_diff_sizes[path],
            }
            for path in sorted(reviewed_diff_hashes)
        },
    }
    if dry_run:
        result = {"status": "dry_run", "would_commit": eligible, **base}
        atomic_write_json(artifact, result)
        return result
    if not eligible:
        result = {
            "status": "skipped",
            "reason": "no unambiguously unrelated paths",
            **base,
        }
        atomic_write_json(artifact, result)
        return result

    latest_statuses, status_error = _snapshot(root_git, root_home)
    latest_sessions = collect_active_session_evidence(root_sessions)
    latest_session_fingerprint = _diff_hash(
        json.dumps(
            latest_sessions,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    if (
        status_error
        or set(latest_statuses) != set(changed)
        or latest_session_fingerprint != session_fingerprint
    ):
        result = {
            "status": "ambiguous",
            "reason": status_error or "path or active-session race before staging",
            **base,
        }
        atomic_write_json(artifact, result)
        return result
    latest_deterministic = classify_dotfile_paths(
        changed, home=root_home, active_sessions=latest_sessions
    )
    if any(path in latest_deterministic for path in eligible):
        result = {
            "status": "ambiguous",
            "reason": "active-session race before staging",
            **base,
        }
        atomic_write_json(artifact, result)
        return result
    try:
        latest_hashes = {
            path: _diff_hash(
                exact_path_diff(path, git_dir=root_git, home=root_home)
            )
            for path in eligible
        }
    except (OSError, RuntimeError, ValueError) as exc:
        result = {
            "status": "ambiguous",
            "reason": f"exact diff race check failed: {str(exc)[:300]}",
            **base,
        }
        atomic_write_json(artifact, result)
        return result
    if latest_hashes != {path: reviewed_diff_hashes[path] for path in eligible}:
        result = {
            "status": "ambiguous",
            "reason": "reviewed diff changed before staging",
            **base,
        }
        atomic_write_json(artifact, result)
        return result

    def _before_push() -> bool:
        current_status, current_error = _snapshot(root_git, root_home)
        current_staged, staged_error = _staged_paths(root_git, root_home)
        if (
            current_error
            or set(current_status) != (set(changed) - set(eligible))
            or staged_error
            or current_staged
        ):
            return False
        current_sessions = collect_active_session_evidence(root_sessions)
        current_fingerprint = _diff_hash(
            json.dumps(
                current_sessions,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        if current_fingerprint != session_fingerprint:
            return False
        current_deterministic = classify_dotfile_paths(
            changed, home=root_home, active_sessions=current_sessions
        )
        return not any(path in current_deterministic for path in eligible)

    commit_result = _commit_exact_paths(
        eligible,
        git_dir=root_git,
        home=root_home,
        expected_dirty_paths=changed,
        expected_diff_hashes=reviewed_diff_hashes,
        push=push,
        pre_push_check=_before_push,
    )
    result = {**base, **commit_result}
    if result.get("status") in {"push_pending", "push_failed", "remote_unverified"}:
        if (
            not isinstance(result.get("pending_push"), Mapping)
            and isinstance(result.get("branch"), str)
            and isinstance(result.get("commit"), str)
        ):
            result["pending_push"] = _pending_push_record(
                result["branch"], result["commit"]
            )
        result.update({
            "phase_status": "failed",
            "phase_failed": True,
            "reason": str(
                result.get("reason")
                or "dotfiles commit was not pushed and requires exact retry"
            )[:2048],
        })
    atomic_write_json(artifact, result)
    print(f"[P9b] {result.get('status')} -> {artifact}")
    return result


__all__ = [name for name in globals() if not name.startswith("__")]
