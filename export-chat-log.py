#!/usr/bin/env python3
"""
Extract a VS Code Copilot chat session from JSONL format into
a clean, readable markdown file.

Usage:
    python3 scripts/export_chat_log.py [--session-id UUID] [--output PATH]

If no session-id is given, uses the most recently active session for this workspace.
If no output is given, writes to agent-logs/ with the standard naming convention.
Use --output - to write markdown to stdout.
"""

from __future__ import annotations


import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import unquote
from typing import Any, cast

# Prepended to workspace-relative links; set via --project-root (default "..")
_project_root = ".."
_workspace_path = ""
_force_insiders = False


# ---------------------------------------------------------------------------
# Workspace / session discovery
# ---------------------------------------------------------------------------

def _vscode_data_dirs() -> list[str]:
    """Return candidate VS Code user-data directories, ordered by preference.

    Checks TERM_PROGRAM_VERSION to prefer Insiders when running inside it.
    Supports macOS, Linux, and Windows.
    """
    variants = ["Code - Insiders", "Code"] if _force_insiders else ["Code", "Code - Insiders"]

    platform = sys.platform
    dirs: list[str] = []
    for variant in variants:
        if platform == "darwin":
            base = os.path.expanduser(f"~/Library/Application Support/{variant}")
        elif platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            base = os.path.join(appdata, variant) if appdata else ""
        else:  # linux / other unix
            config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            base = os.path.join(config, variant)
        if base:
            dirs.append(base)
    return dirs


def find_workspace_storage(workspace_path: str) -> str | None:
    """Find the VS Code workspace storage dir for the given workspace."""
    workspace_uri = "file://" + os.path.abspath(workspace_path)
    for data_dir in _vscode_data_dirs():
        ws_storage = os.path.join(data_dir, "User", "workspaceStorage")
        if not os.path.isdir(ws_storage):
            continue
        for entry in os.listdir(ws_storage):
            ws_json = os.path.join(ws_storage, entry, "workspace.json")
            if os.path.isfile(ws_json):
                try:
                    with open(ws_json) as f:
                        data = json.load(f)
                    if unquote(data.get("folder", "")).rstrip("/") == workspace_uri.rstrip("/"):
                        return os.path.join(ws_storage, entry)
                except (json.JSONDecodeError, IOError):
                    continue
    return None


def get_session_index(storage_path: str) -> dict[str, Any] | None:
    """Read the chat session index from the SQLite state DB."""
    db_path = os.path.join(storage_path, "state.vscdb")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
        )
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def find_rolled_back_request_ids(storage_path: str, session_id: str, all_request_ids: set[str] | None = None) -> set[str]:
    """Detect rolled-back requests using chatEditingSessions timeline.

    VS Code tracks file edits per chat request in a timeline with checkpoints.
    Each checkpoint records an epoch and a requestId.  When the user rolls back
    prompts, the timeline's currentEpoch moves back while the checkpoints
    remain.  Checkpoints with epoch >= currentEpoch are rolled back.

    If the user adds new prompts after rolling back, VS Code deletes the
    rolled-back checkpoints and advances currentEpoch.  In that case, we fall
    back to checkpoint-absence detection: any JSONL request without a matching
    checkpoint is potentially rolled back (retried failures also lack
    checkpoints but are handled separately by classify_requests).

    Returns a set of requestId strings for rolled-back requests, or empty set
    if no rollback data is available.
    """
    state_path = os.path.join(
        storage_path, "chatEditingSessions", session_id, "state.json"
    )
    if not os.path.isfile(state_path):
        return set[str]()
    try:
        with open(state_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return set[str]()

    timeline = data.get("timeline", {})
    cur_epoch = timeline.get("currentEpoch")
    checkpoints = timeline.get("checkpoints", [])

    if not checkpoints:
        return set()

    rolled_back: set[str] = set()

    # Method 1: epoch-based — checkpoints with epoch >= currentEpoch
    if cur_epoch is not None:
        for cp in checkpoints:
            epoch = cp.get("epoch")
            rid = cp.get("requestId")
            if epoch is not None and rid and epoch >= cur_epoch:
                rolled_back.add(rid)

    # Method 2: checkpoint-absence — JSONL requests without any checkpoint
    # This catches rollbacks after new prompts are added (VS Code deletes
    # the rolled-back checkpoints).  Retried failures also lack checkpoints
    # but classify_requests() already handles those with is_retried.
    # Only apply if at least one checkpoint has a requestId; otherwise
    # the heuristic is meaningless (e.g. only an "Initial State" checkpoint).
    if all_request_ids is not None:
        checkpoint_rids = {cp.get("requestId") for cp in checkpoints if cp.get("requestId")}
        if checkpoint_rids:
            for rid in all_request_ids:
                if rid not in checkpoint_rids:
                    rolled_back.add(rid)

    return rolled_back

def find_active_session(storage_path: str, session_id: str | None = None,
                        prefer_recent: bool = False) -> str:
    """Find the session JSONL file path.

    When *prefer_recent* is True (used during --wait mode), prefer the most
    recently **created** session even if the DB still marks it as isEmpty.
    This avoids a race where VS Code hasn't flushed the index yet for a brand-new
    session and the script would otherwise pick an older session.
    """
    sessions_dir = os.path.join(storage_path, "chatSessions")

    if session_id:
        path = os.path.join(sessions_dir, f"{session_id}.jsonl")
        if os.path.isfile(path):
            return path
        raise FileNotFoundError(f"Session {session_id} not found")

    index = get_session_index(storage_path)
    best_nonempty: str | None = None
    newest_created_path: str | None = None
    newest_created_ts: float = 0

    if index:
        entries = index.get("entries", {})
        for sid, info in sorted(
            entries.items(),
            key=lambda x: x[1].get("lastMessageDate", 0),
            reverse=True,
        ):
            path = os.path.join(sessions_dir, f"{sid}.jsonl")
            if not os.path.isfile(path):
                continue
            if best_nonempty is None and not info.get("isEmpty", True):
                best_nonempty = path
            # Track the most recently created session regardless of isEmpty
            timing = info.get("timing", {})
            created: float = timing.get("created", 0)
            if created > newest_created_ts:
                newest_created_ts = created
                newest_created_path = path

    if prefer_recent and newest_created_path is not None and best_nonempty is not None:
        # If a session was created more recently than the best non-empty
        # session's last message, prefer it — it's likely the active session
        # whose isEmpty flag hasn't been flushed yet.
        if index:
            best_sid = os.path.splitext(os.path.basename(best_nonempty))[0]
            best_last_msg: float = index.get("entries", {}).get(best_sid, {}).get("lastMessageDate", 0)
            if newest_created_ts > best_last_msg:
                return newest_created_path

    if best_nonempty is not None:
        return best_nonempty

    # Fallback: most recently modified JSONL
    jsonl_files = sorted(
        (
            (os.path.join(sessions_dir, f), os.path.getmtime(os.path.join(sessions_dir, f)))
            for f in os.listdir(sessions_dir)
            if f.endswith(".jsonl")
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if jsonl_files:
        return jsonl_files[0][0]
    raise FileNotFoundError("No chat sessions found")


# ---------------------------------------------------------------------------
# JSONL replay with response-window collection
# ---------------------------------------------------------------------------

def fingerprint_part(part: dict[str, Any]) -> tuple[str, ...] | None:
    """Return a hashable fingerprint for a response part, or None to skip."""
    kind = part.get("kind", "")

    if kind == "toolInvocationSerialized":
        tcid = part.get("toolCallId", "")
        if tcid:
            return ("tool", tcid)
        return ("tool_hash", hashlib.md5(json.dumps(part, sort_keys=True).encode()).hexdigest())

    if kind == "textEditGroup":
        uri: Any = part.get("uri", {})
        path: str = str(cast(dict[str, Any], uri).get("path", "")) if isinstance(uri, dict) else str(uri)
        return ("edit", path)

    if kind == "inlineReference":
        return None  # don't deduplicate

    if kind == "thinking":
        val = part.get("value", "")
        if not val or not val.strip():
            return None
        # Use id + first 200 chars as fingerprint so that progressively longer
        # versions of the same thinking block are treated as the same part.
        think_id = part.get("id", "")
        prefix = val[:200]
        return ("thinking", think_id, prefix)

    if kind == "mcpServersStarting":
        return None

    # Text part
    val: Any = part.get("value", "")
    if isinstance(val, dict):
        val = str(cast(dict[str, Any], val).get("value", ""))
    if not val or not str(val).strip():
        return None
    return ("text", hashlib.md5(str(val).encode()).hexdigest())


def stitch_response_windows(windows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge overlapping response windows into a single ordered part list."""
    seen: dict[tuple[str, ...], int] = {}   # fingerprint -> index in result
    result: list[dict[str, Any]] = []
    for window in windows:
        for part in window:
            kind = part.get("kind", "")
            if kind == "inlineReference":
                result.append(part)
                continue
            fp = fingerprint_part(part)
            if fp is None:
                continue
            if fp not in seen:
                seen[fp] = len(result)
                result.append(part)
            elif fp[0] == "thinking":
                # Replace with later (more complete) version
                result[seen[fp]] = part
            elif kind == "toolInvocationSerialized":
                # For tool calls (especially subagents), prefer the version
                # with a populated result over an earlier empty one.
                tsd: Any = part.get("toolSpecificData")
                if isinstance(tsd, dict) and cast(dict[str, Any], tsd).get("result"):
                    old_tsd: Any = result[seen[fp]].get("toolSpecificData")
                    old_has_result: bool = bool(
                        isinstance(old_tsd, dict)
                        and cast(dict[str, Any], old_tsd).get("result")
                    )
                    if not old_has_result:
                        result[seen[fp]] = part
    return result


def replay_jsonl(filepath: str) -> dict[str, Any]:
    """Replay the JSONL to reconstruct the full session state."""
    session_state: dict[str, Any] = {}
    requests_by_id: dict[str, dict[str, Any]] = {}
    request_order: list[str] = []
    response_windows: dict[str, list[tuple[int, list[dict[str, Any]]]]] = {}   # rid -> list of (seq, window) pairs
    # Track sequence numbers for interjection detection
    seq = 0
    request_submit_seq: dict[str, int] = {}   # rid -> seq when request was submitted
    # Track result/modelState/followups writes with their seq and origin index
    result_writes: list[tuple[int, int, dict[str, Any]]] = []        # list of (seq, idx, value)
    model_state_writes: list[tuple[int, int, Any]] = []   # list of (seq, idx, value)
    followups_writes: list[tuple[int, int, Any]] = []     # list of (seq, idx, value)

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data: dict[str, Any] = json.loads(line)
            kind: Any = data.get("kind")
            keys: list[Any] = data.get("k", [])
            v: Any = data.get("v")
            seq += 1

            if kind == 0:
                if isinstance(v, dict):
                    session_state = cast(dict[str, Any], v)
                else:
                    session_state = {}
                for req in session_state.get("requests", []):
                    rid: str = req.get("requestId", "")
                    if rid and rid not in requests_by_id:
                        requests_by_id[rid] = req
                        request_order.append(rid)
                        request_submit_seq[rid] = seq
                        resp_val: Any = req.get("response", [])
                        if resp_val:
                            response_windows.setdefault(rid, []).append((seq, resp_val))

            elif kind == 2 and keys == ["requests"]:
                if isinstance(v, list):
                    for req_item in cast(list[Any], v):
                        if not isinstance(req_item, dict):
                            continue
                        req: dict[str, Any] = cast(dict[str, Any], req_item)
                        rid = str(req.get("requestId", ""))
                        if not rid:
                            continue
                        if rid not in requests_by_id:
                            request_order.append(rid)
                            request_submit_seq[rid] = seq
                        requests_by_id[rid] = req
                        resp: list[dict[str, Any]] = list(cast(list[Any], req.get("response", [])))
                        if resp:
                            response_windows.setdefault(rid, []).append((seq, resp))

            elif kind in (1, 2):
                if not keys:
                    continue
                if (
                    keys[0] == "requests"
                    and len(keys) >= 2
                    and isinstance(keys[1], int)
                ):
                    idx: int = keys[1]
                    if idx < len(request_order):
                        rid = request_order[idx]
                        target: dict[str, Any] = requests_by_id[rid]
                        sub_keys: list[Any] = keys[2:]

                        if sub_keys == ["response"] and isinstance(v, list):
                            window: list[dict[str, Any]] = cast(list[dict[str, Any]], v)
                            response_windows.setdefault(rid, []).append((seq, window))
                            target["response"] = window
                            continue

                        # Track result/modelState/followups writes
                        if sub_keys == ["result"] and isinstance(v, dict):
                            result_writes.append((seq, idx, cast(dict[str, Any], v)))
                        elif sub_keys == ["modelState"]:
                            model_state_writes.append((seq, idx, v))
                        elif sub_keys == ["followups"]:
                            followups_writes.append((seq, idx, v))

                        if sub_keys:
                            _apply_nested_update(target, sub_keys, v)
                else:
                    _apply_nested_update(session_state, keys, v)

    # Reassign response windows and results to correct requests
    _reassign_interjections(
        request_order, requests_by_id, request_submit_seq,
        response_windows, result_writes, model_state_writes, followups_writes)

    session_state["requests"] = [requests_by_id[rid] for rid in request_order]
    return session_state


def _apply_nested_update(obj: Any, keys: list[Any], value: Any) -> None:
    """Apply a nested key-path update to obj."""
    for i, k in enumerate(keys[:-1]):
        if isinstance(k, int):
            if isinstance(obj, list):
                obj_l: list[Any] = cast(list[Any], obj)
                while len(obj_l) <= k:
                    obj_l.append({})
                obj = obj_l[k]
            else:
                return
        else:
            nk = keys[i + 1] if i + 1 < len(keys) else None
            if k not in obj:
                obj[k] = [] if isinstance(nk, int) else {}
            obj = obj[k]
    last = keys[-1]
    if isinstance(last, int):
        if isinstance(obj, list):
            obj_l2: list[Any] = cast(list[Any], obj)
            while len(obj_l2) <= last:
                obj_l2.append(None)
            obj_l2[last] = value
    else:
        if isinstance(obj, dict):
            obj[last] = value


def _reassign_interjections(
    request_order: list[str],
    requests_by_id: dict[str, dict[str, Any]],
    request_submit_seq: dict[str, int],
    response_windows: dict[str, list[tuple[int, list[dict[str, Any]]]]],
    result_writes: list[tuple[int, int, dict[str, Any]]],
    model_state_writes: list[tuple[int, int, Any]],
    followups_writes: list[tuple[int, int, Any]],
) -> None:
    """Reassign response windows and results to the correct requests.

    VS Code may write response windows for request N+1 under request N's index
    in the JSONL (an "interjection").  Instead of heuristics, we use the JSONL
    sequence numbers: each response window is assigned to the request with the
    largest submit_seq <= the window's seq.  This naturally handles all
    interjection patterns, including chained ones.

    Results and metadata are similarly reassigned, except that cancel results
    (errorDetails.code == "canceled") stay on their original request.
    """
    if not request_order:
        return

    # Build sorted list of (submit_seq, rid) for binary search
    submit_list = sorted(
        (request_submit_seq[rid], rid) for rid in request_order
    )

    def find_owner(seq_num: int) -> str:
        """Find the request with the largest submit_seq <= seq_num."""
        owner = submit_list[0][1]  # default to first request
        for submit_seq, rid in submit_list:
            if submit_seq <= seq_num:
                owner = rid
            else:
                break
        return owner

    # --- Reassign response windows ---
    # Collect all (seq, window) pairs from all requests
    all_windows: list[tuple[int, list[dict[str, Any]]]] = []  # (seq, window)
    for rid in request_order:
        for seq_num, window in response_windows.get(rid, []):
            all_windows.append((seq_num, window))

    # Sort by seq for chronological order
    all_windows.sort(key=lambda x: x[0])

    # Assign each window to its correct owner
    new_windows: dict[str, list[list[dict[str, Any]]]] = {rid: [] for rid in request_order}
    for seq_num, window in all_windows:
        owner = find_owner(seq_num)
        new_windows[owner].append(window)

    idx_by_rid = {rid: i for i, rid in enumerate(request_order)}
    # Log reassignments
    for rid in request_order:
        orig_count = sum(len(w) for _, w in response_windows.get(rid, []))
        new_count = sum(len(w) for w in new_windows[rid])
        if orig_count != new_count:
            print(f"  Interjection: req[{idx_by_rid[rid]}] response parts {orig_count} -> {new_count}",
                  file=sys.stderr)

    # Stitch and apply
    for rid in request_order:
        requests_by_id[rid]["response"] = stitch_response_windows(new_windows[rid])

    # --- Reassign results, modelState, followups ---
    def reassign_field(writes: list[tuple[int, int, Any]], field_name: str, skip_canceled: bool = True) -> None:
        """Reassign a field based on JSONL write sequence numbers."""
        # Collect (seq, idx, value) -> assign to correct owner
        assignments: dict[str, tuple[int, Any]] = {}  # rid -> (seq, value) — keep latest by seq
        for seq_num, idx, value in writes:
            # Cancel results stay on their original request
            if skip_canceled and field_name == "result" and isinstance(value, dict):
                val_d: dict[str, Any] = cast(dict[str, Any], value)
                err: Any = val_d.get("errorDetails", {})
                if isinstance(err, dict) and cast(dict[str, Any], err).get("code") == "canceled":
                    if idx < len(request_order):
                        orig_rid = request_order[idx]
                        if orig_rid not in assignments or assignments[orig_rid][0] < seq_num:
                            assignments[orig_rid] = (seq_num, value)
                    continue

            owner = find_owner(seq_num)
            if owner not in assignments or assignments[owner][0] < seq_num:
                assignments[owner] = (seq_num, value)

        # Apply: clear field from all requests, then set on assigned owners
        for rid in request_order:
            if rid in assignments:
                requests_by_id[rid][field_name] = assignments[rid][1]
            # Don't clear fields that weren't written via tracked entries
            # (they might have been set in the initial state)

    reassign_field(result_writes, "result")
    reassign_field(model_state_writes, "modelState", skip_canceled=False)

# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def extract_text(val: Any) -> str:
    """Extract plain text from a value that may be a string or {value: ...}."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return str(cast(dict[str, Any], val).get("value", ""))
    return ""


def shorten_path(p: str) -> str:
    """Return workspace-relative path, or the original absolute path if external."""
    if not p:
        return p
    if _workspace_path:
        prefix = _workspace_path + "/"
        if p.startswith(prefix):
            return p[len(prefix):]
    return p  # Keep absolute path as-is for external files


def shorten_paths_in_text(text: str) -> str:
    """Replace workspace-absolute paths with workspace-relative paths in arbitrary text."""
    if not text or not _workspace_path:
        return text
    return text.replace(_workspace_path + "/", "")
def make_link_path(p: str) -> str:
    """Prepend project root to workspace-relative paths for use in markdown links."""
    if not p or p.startswith('/') or p.startswith('~'):
        return p
    return f"{_project_root}/{p}"


def extract_path_from_uris(msg_obj: dict[str, Any]) -> str | None:
    """Extract a shortened file path from a message object with uris."""
    uris: Any = msg_obj.get("uris", {})
    if not isinstance(uris, dict):
        return None
    uris_d: dict[str, Any] = cast(dict[str, Any], uris)
    for _uri_key, uri_obj in uris_d.items():
        if isinstance(uri_obj, dict):
            uri_obj_d: dict[str, Any] = cast(dict[str, Any], uri_obj)
            p: str = str(uri_obj_d.get("path", ""))
            if p:
                return shorten_path(p)
    return None


def clean_message_links(msg: str) -> str:
    """Replace file:/// markdown links with proper relative/absolute links."""
    def _sub(m: re.Match[str]) -> str:
        text = m.group(1)
        # file:/// has 3 slashes; the captured group after file:/// is missing the
        # leading slash for absolute paths, so restore it.
        raw_path = "/" + unquote(m.group(2))
        fragment = ''
        if '#' in raw_path:
            raw_path, frag = raw_path.rsplit('#', 1)
            fragment = '#' + frag
        short = shorten_path(raw_path)
        link_path = make_link_path(short) + fragment
        display = text if text else os.path.basename(raw_path) or short
        return f'[{display}]({link_path})'
    return re.sub(r'\[([^\]]*)\]\(file:///([^)]+)\)', _sub, msg)


def linkify_paths_in_message(msg: str) -> str:
    """Convert backtick-wrapped workspace paths to markdown links with the basename."""
    def _replace(m: re.Match[str]) -> str:
        path: str = m.group(1)
        display_path = re.sub(r'#.*$', '', path)
        basename = os.path.basename(display_path) or display_path
        link = make_link_path(path) if not path.startswith('/') and not path.startswith('~') else path
        return f'[{basename}]({link})'
    return re.sub(r'`([a-zA-Z0-9_.][a-zA-Z0-9_./~-]*\.[a-zA-Z0-9]+[^`]*)`', _replace, msg)


def get_tool_message(part: dict[str, Any]) -> str:
    """Get the best human-readable message for a tool call, with links cleaned."""
    past = part.get("pastTenseMessage", "")
    inv = part.get("invocationMessage", "")
    msg = extract_text(past) or extract_text(inv)
    if msg:
        msg = clean_message_links(msg)
        msg = linkify_paths_in_message(msg)
    return msg.strip() if msg else ""


def humanize_model_id(model_id: str) -> str:
    """Convert a model ID like 'copilot/claude-opus-4.6' to 'Claude Opus 4.6'."""
    if not model_id:
        return ""
    # Strip provider prefix
    name = model_id.split("/")[-1] if "/" in model_id else model_id
    if name == "auto":
        return "Auto"
    # Capitalize each part: claude-opus-4.6 -> Claude Opus 4.6
    parts = name.split("-")
    result: list[str] = []
    for part in parts:
        # Keep version numbers as-is
        if re.match(r'^\d', part):
            result.append(part)
        else:
            result.append(part.capitalize())
    return " ".join(result)


def sanitize_for_markdown(text: str) -> str:
    """Ensure code fences in text are balanced so they don't break the document."""
    lines = text.rstrip().split("\n")
    fence_count = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r'^`{3,}', stripped):
            fence_count += 1
    if fence_count % 2 != 0:
        # Remove the trailing unmatched fence
        for i in range(len(lines) - 1, -1, -1):
            if re.match(r'^`{3,}', lines[i].strip()):
                lines.pop(i)
                break
    return "\n".join(lines)


def fence_for(content: str) -> str:
    """Return a backtick fence string long enough that content can't close it."""
    max_run = 0
    for m in re.finditer(r'`+', content):
        max_run = max(max_run, len(m.group()))
    return '`' * max(3, max_run + 1)

def escape_html(text: str) -> str:
    """Escape HTML special characters in text embedded in markdown headings or blockquotes."""
    return html.escape(text)


def escape_link_text(text: str) -> str:
    """Escape text for use inside a markdown link display: escape HTML and bracket chars."""
    s = html.escape(text)
    return s.replace("[", "&#91;").replace("]", "&#93;")


def md_to_summary_html(text: str) -> str:
    """Convert markdown-formatted text to HTML safe for use inside <summary> tags."""
    result: list[str] = []
    pos = 0
    for m in re.finditer(r'\[([^\]]*)\]\(([^)]*)\)|`([^`]+)`', text):
        result.append(html.escape(text[pos:m.start()]))
        if m.group(3) is not None:
            result.append(f'<code>{html.escape(m.group(3))}</code>')
        else:
            result.append(f'<a href="{m.group(2)}">{html.escape(m.group(1))}</a>')
        pos = m.end()
    result.append(html.escape(text[pos:]))
    return ''.join(result)


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes and simulate carriage-return overwriting."""
    # Simulate CR overwrite: keep last non-empty \r-segment per line
    # (trailing \r leaves an empty final segment that we skip)
    def _cr_last(seg: str) -> str:
        for p in reversed(seg.split('\r')):
            if p:
                return p
        return ''
    text = '\n'.join(_cr_last(seg) for seg in text.split('\n'))
    # Strip all ANSI escape sequences
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def format_list_result(tool_id: str, result_list: list[dict[str, Any]]) -> list[str]:
    """Format a list of URI/match results into markdown list lines."""
    lines: list[str] = []
    seen: set[str] = set()
    for item in result_list:
        if tool_id == "copilot_findFiles":
            path = item.get("path", item.get("fsPath", ""))
            short = shorten_path(path)
            link = make_link_path(short)
            name = os.path.basename(short) or short
            entry = f"- [{name}]({link})"
        elif tool_id == "copilot_findTextInFiles":
            uri = item.get("uri", {})
            path = uri.get("path", uri.get("fsPath", ""))
            short = shorten_path(path)
            link = make_link_path(short)
            name = os.path.basename(short) or short
            line_num = item.get("range", {}).get("startLineNumber", "")
            fragment = f"#L{line_num}" if line_num else ""
            entry = f"- [{name}:{line_num}]({link}{fragment})"
        else:
            continue
        if not path:
            continue
        if entry not in seen:
            seen.add(entry)
            lines.append(entry)
    return lines


def format_hashline_output(result_details: dict[str, Any]) -> list[str]:
    """Format hashline_read resultDetails output, stripping line:hash| prefixes."""
    outputs = result_details.get("output", [])
    if not outputs:
        return []
    raw = outputs[0].get("value", "") if isinstance(outputs[0], dict) else str(outputs[0])
    if not raw:
        return []
    content_lines: list[str] = []
    for line in raw.split("\n"):
        m = re.match(r'^\d+:[a-z]+\|(.*)$', line)
        content_lines.append(m.group(1) if m else line)
    content = "\n".join(content_lines)
    if not content.strip():
        return []
    truncated = len(content) > 4000
    display = content[:4000]
    fence = fence_for(display)
    result = [fence, display]
    if truncated:
        result.append(f"... (truncated, {len(content)} chars)")
    result.append(fence)
    return result




_CONTENT_TXT_RE = re.compile(
    r'chat-session-resources/[^/]+/([^/]+)/content\.txt'
)


def _extract_content_txt_info(part: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
    """If this copilot_readFile reads a content.txt from chat-session-resources,
    return (filesystem_path, start_line, end_line).  Lines are 1-based; None if unknown."""
    ptm: Any = part.get("pastTenseMessage", "")
    inv: Any = part.get("invocationMessage", "")
    for msg_obj in (ptm, inv):
        if not isinstance(msg_obj, dict):
            continue
        val: str = str(cast(dict[str, Any], msg_obj).get("value", ""))
        m = re.search(r'\(file:///([^)]+content\.txt[^)]*)\)', val)
        if m:
            raw_path: str = "/" + unquote(m.group(1))
            # Strip fragment (e.g. #1-1)
            if '#' in raw_path:
                raw_path = raw_path.rsplit('#', 1)[0]
            if _CONTENT_TXT_RE.search(raw_path):
                rm = re.search(r'lines (\d+) to (\d+)', val)
                start_ln: int | None = int(rm.group(1)) if rm else None
                end_ln: int | None = int(rm.group(2)) if rm else None
                return raw_path, start_ln, end_ln
    return None


def _build_tool_call_map(req_meta: Any) -> dict[str, dict[str, str]]:
    """Build a map from tool_call_id -> {name, filePath} from toolCallRounds."""
    result: dict[str, dict[str, str]] = {}
    if not isinstance(req_meta, dict):
        return result
    rounds: list[Any] = cast(dict[str, Any], req_meta).get("toolCallRounds", [])
    if not isinstance(rounds, list):
        return result
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        for tc in cast(list[Any], cast(dict[str, Any], rnd).get("toolCalls", [])):
            if not isinstance(tc, dict):
                continue
            tc_d: dict[str, Any] = cast(dict[str, Any], tc)
            tc_id: str = str(tc_d.get("id", ""))
            tc_name: str = str(tc_d.get("name", ""))
            tc_args_raw: Any = tc_d.get("arguments", "")
            tc_args: dict[str, Any] = {}
            if isinstance(tc_args_raw, str):
                try:
                    parsed = json.loads(tc_args_raw)
                    if isinstance(parsed, dict):
                        tc_args = cast(dict[str, Any], parsed)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif isinstance(tc_args_raw, dict):
                tc_args = cast(dict[str, Any], tc_args_raw)
            fp: str = str(tc_args.get("filePath", ""))
            if tc_id:
                result[tc_id] = {"name": tc_name, "filePath": fp}
    return result


def format_content_txt_read(
    content_txt_path: str,
    tool_call_map: dict[str, dict[str, str]],
    start_line: int | None = None,
    end_line: int | None = None,
) -> list[str] | None:
    """Read a content.txt file from disk and format as a details block.

    Returns formatted markdown lines, or None if the file can't be read.
    Hashline prefixes (e.g. "1:ab|") are preserved so the output reflects
    what the tool actually returned.
    """
    if not os.path.isfile(content_txt_path):
        return None
    try:
        with open(content_txt_path) as f:
            raw: str = f.read()
    except (IOError, OSError):
        return None
    if not raw.strip():
        return None

    # Extract the original tool call ID from the path
    m = _CONTENT_TXT_RE.search(content_txt_path)
    original_tool_id: str = m.group(1) if m else ""
    original_info: dict[str, str] = tool_call_map.get(original_tool_id, {})
    original_name: str = original_info.get("name", "")
    original_file: str = original_info.get("filePath", "")

    # Detect hashline format and slice to the requested line range.
    # Hashline prefixes are kept intact so the output reflects what the tool returned.
    is_hashline: bool = bool(re.match(r'^\d+:[a-z]+\|', raw))

    if is_hashline and start_line is not None and end_line is not None:
        sl: int = start_line
        el: int = end_line
        filtered: list[str] = [
            ln for ln in raw.split("\n")
            if (hl := re.match(r'^(\d+):[a-z]+\|', ln)) and sl <= int(hl.group(1)) <= el
        ]
        content: str = "\n".join(filtered)
    elif not is_hashline and start_line is not None and end_line is not None:
        all_lines: list[str] = raw.split("\n")
        content = "\n".join(all_lines[start_line - 1:end_line])
    else:
        content = raw

    if not content.strip():
        return None

    # Build summary
    if original_file:
        short_file: str = shorten_path(original_file)
        fname: str = os.path.basename(short_file)
        flink: str = make_link_path(short_file)
        if start_line is not None and end_line is not None:
            summary: str = f"Reading lines {start_line}-{end_line} of [{fname}]({flink})"
        else:
            summary = f"Reading all lines of [{fname}]({flink})"
    elif original_name:
        if start_line is not None and end_line is not None:
            summary = f"Tool output ({original_name}), lines {start_line}-{end_line}"
        else:
            summary = f"Tool output ({original_name})"
    else:
        if start_line is not None and end_line is not None:
            summary = f"File content, lines {start_line}-{end_line}"
        else:
            summary = "File content (continued)"

    truncated: bool = len(content) > 4000
    display: str = content[:4000]
    _fence: str = fence_for(display)

    lines: list[str] = []
    lines.append("<details>")
    lines.append(f"<summary>{md_to_summary_html(summary)}</summary>")
    lines.append("")
    lines.append(_fence)
    lines.append(display)
    if truncated:
        lines.append(f"... (truncated, {len(content)} chars)")
    lines.append(_fence)
    lines.append("</details>")
    return lines


# ---------------------------------------------------------------------------
# Tool call formatting
# ---------------------------------------------------------------------------

def format_result_details(result_details: dict[str, Any]) -> list[str]:
    """Format resultDetails as input/output content for inside a collapsed section."""
    if not isinstance(result_details, dict):  # type: ignore[redundant-cast]
        return []
    inp = result_details.get("input", "")
    outputs = result_details.get("output", [])
    is_error = result_details.get("isError", False)
    if not inp and not outputs:
        return []

    lines: list[str] = []
    if is_error:
        lines.append("**(error)**")
    if inp:
        inp_str: str = str(inp)
        inp_display: str = shorten_paths_in_text(inp_str[:3000])
        inp_fence: str = fence_for(inp_display)
        lines.append("**Input:**")
        lines.append(inp_fence)
        lines.append(inp_display)
        if len(inp_str) > 3000:
            lines.append(f"... (truncated, {len(inp_str)} chars)")
        lines.append(inp_fence)
    for out_item in outputs:
        if isinstance(out_item, dict):
            out_d: dict[str, Any] = cast(dict[str, Any], out_item)
            val: str = str(out_d.get("value", ""))
            if val:
                val_display: str = shorten_paths_in_text(val[:3000])
                val_fence: str = fence_for(val_display)
                lines.append("**Output:**")
                lines.append(val_fence)
                lines.append(val_display)
                if len(val) > 3000:
                    lines.append(f"... (truncated, {len(val)} chars)")
                lines.append(val_fence)
    return lines


def format_tool_call(part: dict[str, Any], child_parts: list[dict[str, Any]] | None = None) -> list[str]:
    """Format a tool call into readable markdown lines."""
    tool_id = part.get("toolId", "")
    tsd: Any = part.get("toolSpecificData", {})
    result_details: Any = part.get("resultDetails", {})
    msg: str = get_tool_message(part)
    lines: list[str] = []

    # --- Terminal commands ---
    if tool_id == "run_in_terminal" and isinstance(tsd, dict):
        tsd_d: dict[str, Any] = cast(dict[str, Any], tsd)
        cmd_data: Any = tsd_d.get("commandLine", {})
        cmd = ""
        if isinstance(cmd_data, dict):
            cmd_d: dict[str, Any] = cast(dict[str, Any], cmd_data)
            cmd = str(cmd_d.get("original", "") or cmd_d.get("toolEdited", ""))
        elif isinstance(cmd_data, str):
            cmd = cmd_data
        cmd = str(cmd).strip()
        if not cmd:
            return []

        lines.append("**Terminal:**")
        _cmd_fence = fence_for(cmd)
        lines.append(f"{_cmd_fence}sh")
        lines.append(cmd)
        lines.append(_cmd_fence)

        output_data: Any = tsd_d.get("terminalCommandOutput", {})
        output_text: str = str(cast(dict[str, Any], output_data).get("text", "")) if isinstance(output_data, dict) else ""
        state_data: Any = tsd_d.get("terminalCommandState", {})
        exit_code: Any = cast(dict[str, Any], state_data).get("exitCode") if isinstance(state_data, dict) else None

        if output_text:
            output_lines: list[str] = output_text.rstrip().split("\n")
            max_output = 3000
            truncated = len(output_text) > max_output
            display = strip_ansi(output_text[:max_output].rstrip())
            _out_fence = fence_for(display)

            if len(output_lines) > 4:
                lines.append("<details>")
                summary = f"Output ({len(output_lines)} lines)"
                if exit_code is not None and exit_code != 0:
                    summary += f" \u2014 exit code {exit_code}"
                lines.append(f"<summary>{md_to_summary_html(summary)}</summary>")
                lines.append("")
                lines.append(_out_fence)
                lines.append(display)
                if truncated:
                    lines.append(f"... (truncated, {len(output_text)} chars total)")
                lines.append(_out_fence)
                lines.append("</details>")
            else:
                lines.append(_out_fence)
                lines.append(display)
                lines.append(_out_fence)
                if exit_code is not None and exit_code != 0:
                    lines.append(f"**Exit code:** {exit_code}")
        elif exit_code is not None and exit_code != 0:
            lines.append(f"**Exit code:** {exit_code}")

        return lines

    # --- Todo list ---
    if tool_id == "manage_todo_list":
        if isinstance(tsd, dict) and cast(dict[str, Any], tsd).get("kind") == "todoList":
            todos: Any = cast(dict[str, Any], tsd).get("todoList", [])
            if todos:
                lines.append("**Todo list:**")
                for todo in cast(list[Any], todos):
                    status: str = str(cast(dict[str, Any], todo).get("status", "not-started"))
                    title: str = str(cast(dict[str, Any], todo).get("title", ""))
                    icon = {"completed": "✅", "in-progress": "🔄", "not-started": "⬜"}.get(status, "⬜")
                    lines.append(f"- {icon} {title}")
                return lines
        if msg:
            lines.append(f"*{msg}*")
        return lines

    # --- runSubagent: result text in toolSpecificData ---
    if tool_id == "runSubagent":
        tsd_d = cast(dict[str, Any], tsd) if isinstance(tsd, dict) else {}
        result_text: str = str(tsd_d.get("result", "") or "")
        agent_name: str = str(tsd_d.get("agentName", "") or "")
        model_name: str = str(tsd_d.get("modelName", "") or "")
        prompt_text: str = str(tsd_d.get("prompt", "") or "")
        description: str = str(tsd_d.get("description", "") or "")
        summary_text = msg or description or "Run subagent"

        # Build a header annotation
        header_parts: list[str] = []
        if agent_name:
            header_parts.append(f"{agent_name} agent")
        if model_name:
            header_parts.append(model_name)
        header_annotation: str = " · ".join(header_parts)

        if result_text:
            lines.append("<details>")
            detail_label: str = summary_text
            if header_annotation:
                detail_label = f"{summary_text} ({header_annotation})"
            lines.append(f"<summary>{md_to_summary_html(detail_label)}</summary>")
            lines.append("")
            if prompt_text:
                lines.append("**Prompt:**")
                for pline in prompt_text.strip().split("\n"):
                    lines.append(f"> {pline}")
                lines.append("")
            # Render child tool calls belonging to this subagent
            if child_parts:
                for child in child_parts:
                    child_lines: list[str] = format_tool_call(child)
                    if child_lines:
                        for cl in child_lines:
                            lines.append(cl)
                        lines.append("")
            lines.append(sanitize_for_markdown(result_text.strip()))
            lines.append("")
            lines.append("</details>")
        else:
            if header_annotation:
                lines.append(f"{summary_text} ({header_annotation})")
            else:
                lines.append(summary_text)
            # Render child tool calls even without result text
            if child_parts:
                for child in child_parts:
                    child_lines = format_tool_call(child)
                    if child_lines:
                        for cl in child_lines:
                            lines.append(cl)
                        lines.append("")
        return lines

    # --- File/text search with list results ---
    if tool_id in ("copilot_findFiles", "copilot_findTextInFiles") and isinstance(result_details, list):
        summary_text = msg or f"Used tool: {tool_id}"
        file_lines: list[str] = format_list_result(tool_id, cast(list[dict[str, Any]], result_details))
        if file_lines and len(file_lines) > 4:
            lines.append("<details>")
            lines.append(f"<summary>{md_to_summary_html(summary_text)}</summary>")
            lines.append("")
            lines.extend(file_lines)
            lines.append("</details>")
        elif file_lines:
            lines.append(summary_text)
            lines.extend(file_lines)
        else:
            lines.append(summary_text)
        return lines

    # --- hashline_read: clean hash-prefixed line output ---
    if tool_id == "hashline_read" and isinstance(result_details, dict):
        summary_text = msg or "Read file"
        clean_lines: list[str] = format_hashline_output(cast(dict[str, Any], result_details))
        if clean_lines:
            lines.append("<details>")
            lines.append(f"<summary>{md_to_summary_html(summary_text)}</summary>")
            lines.append("")
            lines.extend(clean_lines)
            lines.append("</details>")
        else:
            lines.append(summary_text)
        return lines

    # --- All other tools: use the message as summary of collapsed input/output ---
    detail_lines: list[str] = format_result_details(cast(dict[str, Any], result_details) if isinstance(result_details, dict) else {})
    summary_text = msg or f"Used tool: {tool_id}"

    if detail_lines:
        lines.append("<details>")
        lines.append(f"<summary>{md_to_summary_html(summary_text)}</summary>")
        lines.append("")
        lines.extend(detail_lines)
        lines.append("</details>")
    else:
        lines.append(summary_text)

    return lines


# ---------------------------------------------------------------------------
# Thinking block formatting
# ---------------------------------------------------------------------------

def format_thinking_block(part: dict[str, Any]) -> list[str]:
    """Format a thinking block as an indented blockquote."""
    val = part.get("value", "")
    if not val or not val.strip():
        return []

    val = sanitize_for_markdown(val.strip())
    quoted = "> 💭 " + val.replace("\n", "\n> ")
    return [quoted]


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def format_inline_ref(part: dict[str, Any]) -> str:
    """Format an inlineReference into a readable file name."""
    name = part.get("name", "")
    if name:
        return f"`{name}`"
    ref: Any = part.get("inlineReference", {})
    if isinstance(ref, dict):
        p: str = str(cast(dict[str, Any], ref).get("path", ""))
        if p:
            return f"`{shorten_path(p)}`"
    return ""


def make_gfm_anchor(text: str) -> str:
    """Generate a GFM-compatible heading anchor from heading text."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'\s+', '-', text.strip())
    text = re.sub(r'-+', '-', text)
    return text


def get_request_model(req: dict[str, Any]) -> str:
    """Get the human-readable model name for a request."""
    model_id = req.get("modelId", "")
    return humanize_model_id(model_id)


def classify_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify requests and assign display turn numbers.

    Returns a list of dicts with keys:
      - req: the original request dict
      - status: 'ok' | 'failed' | 'canceled' | 'incomplete'
      - is_retried: True if a failed request is followed by a retry with same prompt
      - turn_num: display turn number (sequential, skipping retried failures)
    """
    classified: list[dict[str, Any]] = []
    for i, req in enumerate(requests):
        result: Any = req.get("result", {})
        if not isinstance(result, dict):
            result = {}
        result_d: dict[str, Any] = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        error: Any = result_d.get("errorDetails", {})
        if not isinstance(error, dict):
            error = {}
        error_code: str = str(cast(dict[str, Any], error).get("code", "") if isinstance(error, dict) else "")

        if error_code == "failed":
            status = "failed"
        elif error_code == "canceled":
            status = "canceled"
        elif not result and len(req.get("response", [])) > 0:
            status = "incomplete"
        else:
            status = "ok"

        # Check if this failed request was retried (next request has same prompt)
        is_retried = False
        if status == "failed" and i + 1 < len(requests):
            my_text = _get_prompt_text(req)
            next_text = _get_prompt_text(requests[i + 1])
            if my_text and my_text == next_text:
                is_retried = True

        classified.append({
            "req": req,
            "status": status,
            "is_retried": is_retried,
        })

    # Assign turn numbers, skipping retried failures
    turn_num = 0
    for entry in classified:
        if entry["is_retried"]:
            entry["turn_num"] = None  # will be skipped
        else:
            turn_num += 1
            entry["turn_num"] = turn_num

    return classified


def _get_prompt_text(req: dict[str, Any]) -> str:
    msg: Any = req.get("message", {})
    if isinstance(msg, dict):
        return str(cast(dict[str, Any], msg).get("text", "")).strip()
    elif isinstance(msg, str):
        return msg.strip()
    return ""

def session_to_markdown(session: dict[str, Any], rolled_back_ids: set[str] | None = None, source_mtime: float | None = None) -> str:
    """Convert a replayed session state to a rich markdown document.

    source_mtime: mtime of the JSONL file (epoch seconds), used to estimate
                  duration for in-progress requests.
    """
    out: list[str] = []
    if rolled_back_ids is None:
        rolled_back_ids = set()

    title = session.get("customTitle", "Untitled Session")
    dt = get_session_creation_time(session)
    session_model_id = (
        session.get("inputState", {})
        .get("selectedModel", {})
        .get("metadata", {})
        .get("id", "unknown")
    )

    requests = session.get("requests", [])
    classified = classify_requests(requests)
    active = [c for c in classified if not c["is_retried"]]

    # Mark rolled-back entries
    for c in active:
        c["is_rolled_back"] = c["req"].get("requestId") in rolled_back_ids
    visible = [c for c in active if not c["is_rolled_back"]]

    # Compute stats (only for visible requests)
    total_tool_calls = 0
    total_thinking = 0
    total_prompt_tokens = 0
    total_rounds = 0
    total_input_words = 0
    total_output_words = 0
    total_elapsed_ms = 0
    models_used: set[str] = set()
    for c in visible:
        req = c["req"]
        model_name = get_request_model(req)
        if model_name:
            models_used.add(model_name)
        meta_raw: Any = req.get("result", {})
        meta: Any = cast(dict[str, Any], meta_raw).get("metadata", {}) if isinstance(meta_raw, dict) else {}
        if isinstance(meta, dict):
            meta_d2: dict[str, Any] = cast(dict[str, Any], meta)
            if meta_d2.get("promptTokens"):
                total_prompt_tokens += int(meta_d2["promptTokens"])
            rounds: Any = meta_d2.get("toolCallRounds", [])
            total_rounds += max(len(cast(list[Any], rounds)), 1) if rounds or meta_d2.get("promptTokens") else 0
        timings_raw: Any = req.get("result", {})
        timings: Any = cast(dict[str, Any], timings_raw).get("timings", {}) if isinstance(timings_raw, dict) else {}
        if isinstance(timings, dict) and cast(dict[str, Any], timings).get("totalElapsed"):
            total_elapsed_ms += int(cast(dict[str, Any], timings)["totalElapsed"])
        for part in cast(list[Any], req.get("response", [])):
            if isinstance(part, dict):
                part_d: dict[str, Any] = cast(dict[str, Any], part)
                if part_d.get("kind") == "toolInvocationSerialized":
                    total_tool_calls += 1
                if part_d.get("kind") == "thinking":
                    total_thinking += 1

    model_label = "Models" if len(models_used) > 1 else "Model"
    models_str = ', '.join(sorted(models_used)) or humanize_model_id(session_model_id)

    # Compute session time range
    end_dt = dt
    for c in visible:
        req = c["req"]
        req_ts = req.get("timestamp", 0)
        req_elapsed = 0
        timings_raw2: Any = req.get("result", {})
        timings2: Any = cast(dict[str, Any], timings_raw2).get("timings", {}) if isinstance(timings_raw2, dict) else {}
        if isinstance(timings2, dict) and cast(dict[str, Any], timings2).get("totalElapsed"):
            req_elapsed = cast(dict[str, Any], timings2)["totalElapsed"]
        elif c["status"] == "incomplete" and req_ts and source_mtime:
            req_elapsed = source_mtime * 1000 - req_ts
        if req_ts:
            candidate = datetime.fromtimestamp((req_ts + req_elapsed) / 1000)
            if candidate > end_dt:
                end_dt = candidate

    if end_dt.date() == dt.date():
        date_range = f"{dt.strftime('%Y-%m-%d %H:%M')} – {end_dt.strftime('%H:%M')}"
    else:
        date_range = f"{dt.strftime('%Y-%m-%d %H:%M')} – {end_dt.strftime('%Y-%m-%d %H:%M')}"

    out.append(f"# {escape_html(title)}")
    out.append("")
    out.append(f"- **Date:** {date_range}")
    out.append(f"- **Session ID:** {escape_html(str(session.get('sessionId', 'unknown')))}")
    out.append(f"- **{model_label}:** {models_str}")
    out.append(f"- **Turns:** {len(visible)}")
    out.append(f"- **Tool calls:** {total_tool_calls}")
    out.append(f"- **Thinking blocks:** {total_thinking}")
    INPUT_WORDS_IDX = len(out)
    out.append("")  # placeholder for input words
    OUTPUT_WORDS_IDX = len(out)
    out.append("")  # placeholder for output words
    if total_prompt_tokens:
        out.append(f"- **Prompt tokens (last round):** {total_prompt_tokens:,}")
    if total_rounds:
        out.append(f"- **API rounds:** {total_rounds:,}")
    if total_elapsed_ms:
        out.append(f"- **Total elapsed:** {total_elapsed_ms / 1000:.0f}s")
    out.append("")

    # --- Table of Contents ---
    out.append("## Table of Contents")
    out.append("")
    for c in visible:
        req = c["req"]
        turn_idx = c["turn_num"]
        user_text = _get_prompt_text(req)

        # Create a short preview of the prompt
        preview = user_text.split("\n")[0] if user_text else "(empty)"
        if len(preview) > 100:
            preview = preview[:97] + "..."

        status_marker = ""
        if c["status"] == "canceled":
            status_marker = " ⚠️ canceled"
        elif c["status"] == "incomplete":
            status_marker = " ⚠️ incomplete"
        elif c["status"] == "failed":
            status_marker = " ⚠️ failed"

        model_name = get_request_model(req)
        model_suffix = f" ({model_name})" if model_name else ""

        anchor = make_gfm_anchor(f"User ({turn_idx})")
        out.append(f"{turn_idx}. [{escape_link_text(preview)}](#{anchor}){model_suffix}{status_marker}")
    out.append("")
    out.append("---")
    out.append("")

    # --- Turns ---
    rollback_count = 0
    for c in active:
        if c["is_rolled_back"]:
            rollback_count += 1
            continue
        # Emit rollback marker if we just passed a sequence of rolled-back turns
        if rollback_count > 0:
            s = "s" if rollback_count > 1 else ""
            out.append(f"**{rollback_count} user prompt{s} rolled back**")
            out.append("")
            out.append("---")
            out.append("")
            rollback_count = 0

        req = c["req"]
        turn_idx = c["turn_num"]
        user_text = _get_prompt_text(req)
        turn_input_words = len(user_text.split()) if user_text else 0
        turn_output_words = 0

        req_ts = req.get("timestamp", 0)
        req_dt = datetime.fromtimestamp(req_ts / 1000) if req_ts else None
        req_timings_raw: Any = req.get("result", {})
        req_timings: Any = cast(dict[str, Any], req_timings_raw).get("timings", {}) if isinstance(req_timings_raw, dict) else {}
        req_elapsed_ms: Any = cast(dict[str, Any], req_timings).get("totalElapsed") if isinstance(req_timings, dict) else None
        # For incomplete requests, estimate elapsed from file mtime
        if req_elapsed_ms is None and c["status"] == "incomplete" and req_ts and source_mtime:
            req_elapsed_ms = source_mtime * 1000 - req_ts
        req_meta_raw: Any = req.get("result", {})
        req_meta: Any = cast(dict[str, Any], req_meta_raw).get("metadata", {}) if isinstance(req_meta_raw, dict) else {}
        req_pt: Any = cast(dict[str, Any], req_meta).get("promptTokens") if isinstance(req_meta, dict) else None
        req_rounds: int = len(cast(list[Any], cast(dict[str, Any], req_meta).get("toolCallRounds", []))) if isinstance(req_meta, dict) else 0

        anchor = make_gfm_anchor(f"User ({turn_idx})")
        out.append(f'<a id="{anchor}"></a>')
        out.append("")
        out.append(f"## User ({turn_idx})")
        out.append("")
        if user_text:
            quoted_user = "> " + escape_html(user_text.strip()).replace("\n", "\n> ")
            out.append(quoted_user)
        else:
            out.append("> *(empty message)*")
        out.append("")

        # Status note for problematic requests
        if c["status"] == "canceled":
            out.append("> **⚠️ This request was canceled.**")
            out.append("")
        elif c["status"] == "incomplete":
            out.append("> **⚠️ This response did not complete.**")
            out.append("")
        elif c["status"] == "failed":
            out.append("> **⚠️ This request failed.**")
            out.append("")

        # User prompt timestamp
        if req_dt:
            out.append(f"*{req_dt.strftime('%Y-%m-%d %H:%M')}*")
            out.append("")

        response = req.get("response", [])
        if not isinstance(response, list) or not response:
            continue

        model_name = get_request_model(req)
        model_suffix = f" — {model_name}" if model_name else ""

        out.append(f"### Assistant{model_suffix}")
        out.append("")
        # Accumulate text runs (text + inline refs) and flush on tool calls / thinking
        text_run: list[str] = []

        def flush_text_run() -> None:
            if text_run:
                merged = "".join(text_run).strip()
                if merged:
                    out.append(merged)
                    out.append("")
                text_run.clear()

        # Pre-scan: build tool-call map from toolCallRounds for content.txt resolution
        tcr_map: dict[str, dict[str, str]] = _build_tool_call_map(req_meta)

        # Pre-scan: collect tool calls that belong to a subagent
        subagent_children: dict[str, list[dict[str, Any]]] = {}  # parent tcid -> child parts
        subagent_child_ids: set[str] = set()  # toolCallIds that are children
        for _part in cast(list[Any], response):
            if not isinstance(_part, dict):
                continue
            _part_d: dict[str, Any] = cast(dict[str, Any], _part)
            parent_id: str = str(_part_d.get("subAgentInvocationId", "") or "")
            if parent_id:
                subagent_children.setdefault(parent_id, []).append(_part_d)
                child_tcid: str = str(_part_d.get("toolCallId", "") or "")
                if child_tcid:
                    subagent_child_ids.add(child_tcid)

        for part in cast(list[Any], response):
            if not isinstance(part, dict):
                continue

            part_d2: dict[str, Any] = cast(dict[str, Any], part)
            part_kind = part_d2.get("kind", "")

            if part_kind in ("mcpServersStarting", "textEditGroup"):
                continue

            # Thinking blocks — render as collapsed sections
            if part_kind == "thinking":
                flush_text_run()
                think_lines: list[str] = format_thinking_block(part_d2)
                if think_lines:
                    for line in think_lines:
                        out.append(line)
                    out.append("")
                think_text: str = str(part_d2.get("value", ""))
                turn_output_words += len(think_text.split()) if think_text else 0
                continue

            # Inline reference — merge into text run
            if part_kind == "inlineReference":
                ref_text: str = format_inline_ref(part_d2)
                if ref_text:
                    text_run.append(ref_text)
                continue

            # Tool call — flush text, then render tool
            tool_id: str = str(part_d2.get("toolId", ""))
            if part_kind == "toolInvocationSerialized" or (tool_id and part_kind != ""):
                # Skip tool calls that belong to a subagent (rendered with parent)
                part_tcid: str = str(part_d2.get("toolCallId", "") or "")
                if part_tcid in subagent_child_ids:
                    continue
                flush_text_run()

                # Handle copilot_readFile for content.txt (large tool result continuation)
                if tool_id == "copilot_readFile":
                    ct_info: tuple[str, int | None, int | None] | None = _extract_content_txt_info(part_d2)
                    if ct_info is not None:
                        ct_fpath, ct_start, ct_end = ct_info
                        ct_lines: list[str] | None = format_content_txt_read(ct_fpath, tcr_map, ct_start, ct_end)
                        if ct_lines:
                            for line in ct_lines:
                                out.append(line)
                            out.append("")
                        continue

                # For subagent invocations, pass their child tool calls
                children: list[dict[str, Any]] | None = subagent_children.get(part_tcid)
                tool_lines: list[str] = format_tool_call(part_d2, children)
                if tool_lines:
                    for line in tool_lines:
                        out.append(line)
                    out.append("")
                # Tool output = input words (counted from response parts)
                rd: Any = part_d2.get("resultDetails", {})
                if isinstance(rd, dict):
                    rd_d: dict[str, Any] = cast(dict[str, Any], rd)
                    for out_item in cast(list[Any], rd_d.get("output", [])):
                        if isinstance(out_item, dict):
                            val_s: str = str(cast(dict[str, Any], out_item).get("value", ""))
                            turn_input_words += len(val_s.split()) if val_s else 0
                elif isinstance(rd, list):
                    for item in cast(list[Any], rd):
                        if isinstance(item, dict):
                            item_d: dict[str, Any] = cast(dict[str, Any], item)
                            val_s2: str = str(item_d.get("value", "") or item_d.get("path", ""))
                            turn_input_words += len(val_s2.split()) if val_s2 else 0
                continue

            # Text content — add to text run
            val2: Any = part_d2.get("value", "")
            text: str = extract_text(val2)
            if text and text.strip():
                text_run.append(text)
                turn_output_words += len(text.split())

        flush_text_run()
        # Count tool call arguments from toolCallRounds (output words)
        if isinstance(req_meta, dict):
            req_meta_d2: dict[str, Any] = cast(dict[str, Any], req_meta)
            for rnd in cast(list[Any], req_meta_d2.get("toolCallRounds", [])):
                if not isinstance(rnd, dict):
                    continue
                rnd_d: dict[str, Any] = cast(dict[str, Any], rnd)
                for tc in cast(list[Any], rnd_d.get("toolCalls", [])):
                    if isinstance(tc, dict):
                        args: str = str(cast(dict[str, Any], tc).get("arguments", ""))
                        turn_output_words += len(args.split()) if args else 0
        total_input_words += turn_input_words
        total_output_words += turn_output_words

        # Response end metadata
        resp_meta_parts: list[str] = []
        if req_elapsed_ms is not None and req_ts:
            end_dt = datetime.fromtimestamp((req_ts + req_elapsed_ms) / 1000)
            resp_meta_parts.append(end_dt.strftime('%Y-%m-%d %H:%M'))
        elif req_dt:
            resp_meta_parts.append(req_dt.strftime('%Y-%m-%d %H:%M'))
        if req_elapsed_ms is not None:
            resp_meta_parts.append(f"{req_elapsed_ms / 1000:.0f}s")
        if c["status"] == "incomplete":
            resp_meta_parts.append("in progress")
        resp_word_parts: list[str] = []
        if turn_input_words:
            resp_word_parts.append(f"{turn_input_words:,} in")
        if turn_output_words:
            resp_word_parts.append(f"{turn_output_words:,} out")
        if resp_word_parts:
            resp_meta_parts.append("Words: " + " \u00b7 ".join(resp_word_parts))
        resp_token_parts: list[str] = []
        if req_pt:
            resp_token_parts.append(f"{req_pt:,} ctx")
        if req_rounds > 1:
            resp_token_parts.append(f"{req_rounds} rounds")
        if resp_token_parts:
            resp_meta_parts.append(" · ".join(resp_token_parts))
        if resp_meta_parts:
            sep = " \u00b7 "
            out.append("*" + sep.join(resp_meta_parts) + "*")
            out.append("")

        out.append("---")
        out.append("")
    # Trailing rollback marker if session ends with rolled-back turns
    if rollback_count > 0:
        s = "s" if rollback_count > 1 else ""
        out.append(f"**{rollback_count} user prompt{s} rolled back**")
        out.append("")
        out.append("---")
        out.append("")

    # Fill in word count placeholders now that we've counted everything
    out[INPUT_WORDS_IDX] = f"- **Input words:** {total_input_words:,}"
    out[OUTPUT_WORDS_IDX] = f"- **Output words:** {total_output_words:,}"

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def get_model_short_name(session: dict[str, Any]) -> str:
    model_meta = (
        session.get("inputState", {})
        .get("selectedModel", {})
        .get("metadata", {})
    )
    return model_meta.get("id", "unknown")


def get_session_creation_time(session: dict[str, Any]) -> datetime:
    ts = session.get("creationDate", 0)
    if ts > 0:
        return datetime.fromtimestamp(ts / 1000)
    return datetime.now()


def generate_output_path(session: dict[str, Any], output_dir: str) -> str:
    dt = get_session_creation_time(session)
    date_str = dt.strftime("%Y-%m-%d_%H-%M")
    filename = f"{date_str}_log.md"
    return os.path.join(output_dir, filename)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Export VS Code chat session to markdown")
    parser.add_argument("--session-id", help="Specific session UUID to export")
    parser.add_argument("--output", "-o", help="Output file path (use '-' for stdout)")
    parser.add_argument("--workspace", "-w", default=".", help="Workspace root directory")
    parser.add_argument("--project-root", default="..", help="Path from output dir to project root, prepended to relative links (default: '..')")
    parser.add_argument("--list", "-l", action="store_true", help="List available sessions")
    parser.add_argument("--wait", action="store_true", default=None, help="Wait for JSONL flush before exporting (default: on if stdin is not a TTY)")
    parser.add_argument("--insiders", action="store_true", help="Force using VS Code Insiders data directory")
    parser.add_argument("--no-insiders", action="store_true", help="Do not use VS Code Insiders data directory (overrides TERM_PROGRAM_VERSION detection)")
    args = parser.parse_args()
    if args.wait is None:
        args.wait = not sys.stdin.isatty()

    global _project_root, _workspace_path, _force_insiders
    _project_root = args.project_root
    _workspace_path = os.path.abspath(args.workspace)
    if args.insiders:
        _force_insiders = True
    elif args.no_insiders:
        _force_insiders = False
    else:
        _force_insiders = "insider" in os.environ.get("TERM_PROGRAM_VERSION", "").lower()

    workspace = os.path.abspath(args.workspace)
    storage_path = find_workspace_storage(workspace)
    if not storage_path:
        print(f"Error: Could not find VS Code workspace storage for {workspace}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        index = get_session_index(storage_path)
        if index:
            entries = index.get("entries", {})
            for sid, info in sorted(entries.items(), key=lambda x: x[1].get("lastMessageDate", 0) if isinstance(x[1], dict) else 0, reverse=True):
                info_d: dict[str, Any] = cast(dict[str, Any], info)
                ts: Any = info_d.get("lastMessageDate", 0)
                dt = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S') if ts else "?"
                title: str = str(info_d.get("title", "Untitled"))
                _empty: Any = info_d.get("isEmpty", True)
                print(f"  {sid}  {dt}  {title}")
        return

    session_path = find_active_session(storage_path, args.session_id,
                                        prefer_recent=bool(args.wait))
    print(f"Extracting session from: {os.path.basename(session_path)}", file=sys.stderr)

    # Wait for JSONL flush — VS Code writes chat data every ~60 seconds.
    # Waiting for the next write ensures we capture response parts that
    # have been generated but not yet persisted.  Also handles the case
    # where a brand-new session's JSONL has only the initial snapshot (1
    # line) and we need to wait for actual content to appear.
    if args.wait:
        sys.stdin.close()
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        initial_mtime = os.path.getmtime(session_path)
        deadline = time.time() + 65
        print("  Waiting for JSONL flush...", end="", file=sys.stderr, flush=True)
        while time.time() < deadline:
            if os.path.getmtime(session_path) != initial_mtime:
                break
            time.sleep(0.5)
        elapsed_wait = 65 - (deadline - time.time())
        if os.path.getmtime(session_path) != initial_mtime:
            print(f" flushed after {elapsed_wait:.0f}s", file=sys.stderr)
        else:
            print(f" timeout (file unchanged)", file=sys.stderr)

    session = replay_jsonl(session_path)

    # Detect rolled-back requests
    session_id = session.get("sessionId", os.path.splitext(os.path.basename(session_path))[0])
    all_request_ids = {r.get("requestId") for r in session.get("requests", []) if r.get("requestId")}
    rolled_back_ids = find_rolled_back_request_ids(storage_path, session_id, all_request_ids)
    if rolled_back_ids:
        print(f"  Detected {len(rolled_back_ids)} rolled-back request(s)", file=sys.stderr)

    title = session.get("customTitle", "Untitled")
    model = get_model_short_name(session)
    dt = get_session_creation_time(session)
    n_requests = len(session.get("requests", []))
    print(f"  Title: {title}", file=sys.stderr)
    print(f"  Model: {model}", file=sys.stderr)
    print(f"  Created: {dt}", file=sys.stderr)
    print(f"  Requests: {n_requests}", file=sys.stderr)

    total_parts = sum(len(r.get("response", [])) for r in session.get("requests", []))
    print(f"  Stitched parts: {total_parts}", file=sys.stderr)

    source_mtime = os.path.getmtime(session_path)
    markdown = session_to_markdown(session, rolled_back_ids=rolled_back_ids,
                                   source_mtime=source_mtime)

    if args.output == "-":
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")
        print("  Written to: stdout", file=sys.stderr)
        print(f"  Size: {len(markdown)} chars, {markdown.count(chr(10))} lines", file=sys.stderr)
        return

    if args.output:
        output_path = args.output
    else:
        output_dir = os.path.join(workspace, "agent-logs")
        os.makedirs(output_dir, exist_ok=True)
        output_path = generate_output_path(session, output_dir)

    with open(output_path, "w") as f:
        f.write(markdown)

    print(f"  Written to: {output_path}", file=sys.stderr)
    print(f"  Size: {len(markdown)} chars, {markdown.count(chr(10))} lines", file=sys.stderr)


if __name__ == "__main__":
    main()
