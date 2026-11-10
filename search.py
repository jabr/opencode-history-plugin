#!/usr/bin/env python3
"""history-search — semantic search over OpenCode conversation history.

The OpenCode SQLite database IS the corpus: a semble Source
(https://github.com/jabr/semble, branch source-abstraction) scans
`session_message` rows joined to `session_v2` for one project and renders
each message's `data` JSON as a Markdown transcript block, so no corpus is
ever materialized on disk. Per-project indexes are cached at
~/.cache/opencode/history-search/<sha256>/index-docs/ and refreshed
incrementally — only changed messages are re-read and re-embedded, and
messages deleted from the DB fall out of the index on the next refresh.

Scoping is enforced in retrieval, not by post-filtering:
- project scope: the index only ever contains that project's messages
- session scope: same index, semble's pre-ranking selector (filter_paths)
  restricts retrieval to that session's documents before top-k

Two faces, one tool: human rendering by default (the terminal twin of the
history_search plugin tool), --json for programmatic consumers (the plugin).

Usage:
  history-search [-k N] [-j] "query"
  history-search -m <msgID>            expand one message instead of searching
  history-search --prewarm              refresh the index without searching
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "opencode" / "history-search"


def project_key(project_id: str) -> str:
    """Per-project index key: an absolute synthetic path, because semble hashes
    non-URL keys after resolving them against the cwd — a relative-looking key
    would silently split the cache across working directories."""
    return f"/opencode-history/{project_id}"


def die(message: str) -> None:
    print(f"history-search: {message}", file=sys.stderr)
    sys.exit(1)


def open_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        die(f"database not found: {path} (pass --db?)")
    # Read-only URI: WAL mode lets this run safely against the live server.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ---------------------------------------------------------------------------
# Transcript rendering — port of the old TS transcriptBlock (the DB `data`
# JSON has the same shape the API returned). One document per message means a
# hit maps back to its message by ID, and a chunk can never span two messages.
# ---------------------------------------------------------------------------


def render_block(msg_id: str, msg_type: str, data: dict) -> str:
    header = f"## {msg_id} [{msg_type}]"
    if msg_type == "user":
        return f"{header}\nuser: {data.get('text', '')}"
    if msg_type == "synthetic":
        # Fresh sends embed the provenance frame in the text itself; older
        # messages fall back to the metadata-derived label.
        text = data.get("text", "")
        if not text.startswith("[agent-message"):
            sender = (data.get("metadata") or {}).get("from")
            text = f"agent-message{f' from {sender}' if sender else ''}: {text}"
        return f"{header}\n{text}"
    if msg_type == "system":
        return f"{header}\nsystem: {data.get('text', '')}"
    if msg_type == "assistant":
        # Full transcripts: text and reasoning verbatim, tool calls with their
        # inputs and results — "what was actually done" is often exactly what a
        # later query is looking for.
        parts = []
        for part in data.get("content") or []:
            kind = part.get("type")
            if kind == "text":
                parts.append(part.get("text", ""))
            elif kind == "reasoning":
                parts.append(f"[reasoning] {part.get('text', '')}")
            else:
                state = part.get("state") or {}
                call = json.dumps(state["input"]) if "input" in state else ""
                results = "\n".join(
                    item.get("text", "") if item.get("type") == "text" else f"[{item.get('type')}]"
                    for item in state.get("content") or []
                )
                parts.append(f"[tool:{part.get('name')}] {call}\n{results}")
        return f"{header}\nassistant({data.get('agent', '')}):\n" + "\n".join(parts)
    # Compaction summaries, shells, switches, … — keep the raw shape so
    # nothing searchable is silently dropped.
    return f"{header}\n{json.dumps(data, default=str)}"


class OpenCodeHistorySource:
    """A semble Source over one project's session messages in the opencode DB."""

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.conn = conn
        self.project_id = project_id
        self.read_count = 0  # read() runs once per changed doc — the changed-count stat

    def scan(self):
        """Yield (doc_id, version): doc per message, version = time_updated."""
        rows = self.conn.execute(
            "SELECT m.session_id, m.id, m.time_updated"
            " FROM session_message m JOIN session_v2 s ON s.id = m.session_id"
            " WHERE s.project_id = ?",
            (self.project_id,),
        ).fetchall()
        return iter([(f"{sid}/{mid}.md", str(updated)) for sid, mid, updated in rows])

    def read(self, doc_id: str) -> str:
        self.read_count += 1
        session_id, rest = doc_id.split("/", 1)
        msg_id = rest.removesuffix(".md")
        row = self.conn.execute(
            "SELECT type, data FROM session_message WHERE session_id = ? AND id = ?",
            (session_id, msg_id),
        ).fetchone()
        if row is None:
            raise KeyError(doc_id)
        try:
            data = json.loads(row[1])
        except ValueError:
            data = None
        if isinstance(data, dict):
            return render_block(msg_id, row[0], data) + "\n"
        return f"## {msg_id} [raw]\n{row[1]}\n"


@contextmanager
def file_lock(path: Path):
    """Serialize index refreshes per project (plugin + terminal may race)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_project_index(conn: sqlite3.Connection, project_id: str, cache_root: Path, refresh: bool = True):
    """The persisted per-project index, refreshed incrementally under a lock."""
    # Imported lazily: -m and error paths never pay model/import cost, and
    # SEMBLE_CACHE_LOCATION (set in main) is read at call time.
    from semble import ContentType, SembleIndex
    from semble.cache import find_index_from_cache_folder, load_previous_for_incremental
    from semble.index.create import create_index_from_source
    from semble.index.dense import load_model

    content = (ContentType.DOCS,)
    index_dir = find_index_from_cache_folder(project_key(project_id), content)
    if not refresh:
        if not index_dir.exists():
            die("no cached index for this project yet — run a normal search to build it")
        try:
            index = SembleIndex.load_from_disk(index_dir)
        except (OSError, ValueError):
            die("--no-refresh set and the cached index is unusable; run a normal search to rebuild it")
        return index, {"cache": "hit", "documents": len(index._manifest), "changed": 0, "build_ms": 0}

    model, model_path = load_model(None)
    source = OpenCodeHistorySource(conn, project_id)
    lock_path = cache_root / f"index-{re.sub(r'[^A-Za-z0-9._-]', '_', project_id)}.lock"
    with file_lock(lock_path):
        previous = load_previous_for_incremental(project_key(project_id), None, content)
        if previous is None:
            count = conn.execute(
                "SELECT count(*) FROM session_message m"
                " JOIN session_v2 s ON s.id = m.session_id WHERE s.project_id = ?",
                (project_id,),
            ).fetchone()[0]
            print(
                f"history-search: cold index build for {count} messages (one-time; incremental after)",
                file=sys.stderr,
            )
        started = time.perf_counter()
        bm25, semantic, chunks, manifest = create_index_from_source(source, model, previous=previous)
        index = SembleIndex(model, bm25, semantic, chunks, model_path, content=content, manifest=manifest)
        if source.read_count > 0:
            index.save(index_dir)  # nothing changed → skip the disk churn
    changed = source.read_count
    return index, {
        "cache": "cold" if previous is None else ("hit" if changed == 0 else "incremental"),
        "documents": len(manifest),
        "changed": changed,
        "build_ms": round((time.perf_counter() - started) * 1000),
    }


def load_all_indexes(conn: sqlite3.Connection, cache_root: Path):
    """Unknown cwd: best-effort search over every project's persisted index."""
    from semble import ContentType, SembleIndex
    from semble.cache import find_index_from_cache_folder

    content = (ContentType.DOCS,)
    loaded = []
    for (project_id,) in conn.execute("SELECT DISTINCT project_id FROM session_v2"):
        index_dir = find_index_from_cache_folder(project_key(project_id), content)
        if not index_dir.exists():
            continue
        try:
            loaded.append((project_id, SembleIndex.load_from_disk(index_dir)))
        except (OSError, ValueError):
            continue  # partial/corrupt — a normal in-project search rebuilds it
    if not loaded:
        die("no indexed history yet — run a search inside a project first (or pass --project)")
    try:
        index = SembleIndex.merge(loaded)
    except ValueError as err:
        die(f"cannot merge project indexes: {err}")
    return index, {"cache": "merged", "documents": len(index.chunks), "changed": 0, "build_ms": 0}


def resolve_project(conn: sqlite3.Connection) -> str | None:
    """Longest matching ancestor wins: a cwd deep inside a project resolves to
    that project, not just any prefix that happens to contain it. Tiered,
    most-authoritative first:
    1. `worktree` — the current registry (opencode's own resolve persists here;
       `project_directory` is its deprecated predecessor with no remaining
       readers or writers in core, so its rows are stale by design).
    2. `project_directory` — kept for legacy rows predating `worktree`.
    3. `session_v2.directory` — the only complete record; directories that were
       never registered at all resolve through the most recent session run
       there, since a directory can span projects (e.g. `global` first, then a
       proper project once registered)."""
    cwd = str(Path.cwd())
    row = conn.execute(
        "SELECT project_id FROM worktree WHERE ? LIKE directory || '%'"
        " ORDER BY length(directory) DESC, time_created DESC LIMIT 1",
        (cwd,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT project_id FROM project_directory WHERE ? LIKE directory || '%'"
        " ORDER BY length(directory) DESC LIMIT 1",
        (cwd,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT project_id FROM session_v2 WHERE ? LIKE directory || '%'"
        " ORDER BY length(directory) DESC, time_updated DESC LIMIT 1",
        (cwd,),
    ).fetchone()
    return row[0] if row else None


def session_doc_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [
        f"{session_id}/{row[0]}.md"
        for row in conn.execute("SELECT id FROM session_message WHERE session_id = ?", (session_id,))
    ]


def result_rows(conn: sqlite3.Connection, results, scope: dict) -> list[dict]:
    """IDs that vary per hit, plus the same enrichment the human face shows:
    the owning project's directory (cross-project only), the session title,
    the message type, and its local timestamp. A model reading JSON gets the
    context a human gets from the markdown — which project/conversation/when
    a hit is from — without a second lookup per hit. Title/type/time are
    enrichment: lookups are guarded and omitted (not nulled) when unknown, so
    they can never break the listing."""
    rows = []
    for result in results:
        chunk = result.chunk
        parts = chunk.file_path.split("/")
        row: dict = {}
        if scope["project"] is None:
            # Merged cross-project indexes prefix chunk paths with the project
            # ID (3 segments); single-project indexes do not (2 segments).
            project_id = parts[0] if len(parts) == 3 else None
            if project_id:
                row["projectID"] = project_id
                # The project ID hash means nothing to a reader — carry the
                # directory label the human face prints. Named projectDir to
                # stay clear of projectID/scope.project (IDs).
                row["projectDir"] = project_label(conn, project_id)
        if scope["session"] is None:
            row["sessionID"] = parts[-2] if len(parts) >= 2 else ""
        row["messageID"] = parts[-1].removesuffix(".md")
        info = conn.execute(
            "SELECT s.title, m.type, m.time_created FROM session_message m"
            " JOIN session_v2 s ON s.id = m.session_id WHERE m.id = ?",
            (row["messageID"],),
        ).fetchone()
        if info:
            title, mtype, created = info
            if title:
                row["title"] = title
            if mtype:
                row["type"] = mtype
                if isinstance(created, int) and created > 0:
                    row["time"] = time.strftime("%b %e %Y, %H:%M", time.localtime(created / 1000))
        row["score"] = result.score
        row["snippet"] = chunk.content
        rows.append(row)
    return rows


def show_text(text: str) -> None:
    """Render text through mdcat when it actually works, else print it raw.
    mdcat exits 0 even when it fails to run (e.g. an unreadable config in a
    sandboxed shell), so the check is a tiny canary render — not the exit
    code, which would silently swallow the output."""
    if shutil.which("mdcat"):
        canary = subprocess.run(["mdcat"], input="#\n", text=True, capture_output=True)
        if canary.returncode == 0 and canary.stdout:
            subprocess.run(["mdcat"], input=text, text=True)
            return
    print(text, end="")


def project_label(conn: sqlite3.Connection, project_id: str) -> str:
    """Human-friendly project label: its shortest registered directory."""
    row = conn.execute(
        "SELECT directory FROM project_directory WHERE project_id = ?"
        " ORDER BY length(directory) LIMIT 1",
        (project_id,),
    ).fetchone()
    if not row or not row[0]:
        return project_id
    return row[0].replace(str(Path.home()), "~", 1)


def render_human(query: str, rows: list[dict], scope: dict) -> None:
    if not rows:
        print(f"no matches for: {query}")
        return
    # One markdown section per hit: the session title as the heading, a meta
    # line with the copyable message ID (the handle for -m), then a capped
    # excerpt as an indented code block — indented so markdown inside the
    # message itself stays inert. All metadata comes from result_rows, whose
    # guarded lookups already omit anything unknown, so nothing here can
    # break the listing.
    doc = []
    for i, row in enumerate(rows, 1):
        meta = [f"`{row['messageID']}`"]
        if row.get("projectDir"):
            meta.append(row["projectDir"])
        if row.get("type"):
            meta.append(row["type"])
            if row.get("time"):
                meta.append(row["time"])
        meta.append(f"score {row['score']:.3f}")
        doc.append(
            f"### {i}. {row.get('title') or row.get('sessionID') or scope.get('session') or row['messageID']}\n\n{' · '.join(meta)}\n"
        )
        # Marker-only chunks (a hit on just the `## msg_…` header line) have no
        # excerpt worth printing.
        body = [line for line in row["snippet"].splitlines() if not line.startswith("## msg_")]
        if any(line.strip() for line in body):
            block = []
            for line in body[:14]:
                if len(line) > 200:
                    line = line[:200] + "…"
                block.append(f"    {line}")
            if len(body) > 14:
                block.append("    …")
            doc.append("\n".join(block) + "\n")
    show_text("\n".join(doc))


def emit(args, query: str, rows: list[dict], scope: dict, stats: dict) -> None:
    if args.json:
        print(
            json.dumps(
                {"query": query, "scope": scope, "results": rows, "index": stats},
                separators=(",", ":"),
            )
        )
        return
    render_human(query, rows, scope)


def run_search(args, cache_root: Path) -> None:
    conn = open_db(Path(args.db).expanduser())
    # 'all' is the explicit cross-project opt-in; it takes the same merged
    # path as an unresolvable cwd (scope.project null marks both).
    project = None if args.project == "all" else (args.project or resolve_project(conn))
    scope = {"project": project, "session": args.session}
    stats: dict = {}
    if project is None:
        if args.session:
            die("--session needs a single project scope — pass --project <id> (not 'all')")
        index, stats = load_all_indexes(conn, cache_root)
        doc_ids = None
    else:
        if args.session:
            # Scope policy: a session belongs to exactly one project; opting
            # into another project's history is explicit.
            row = conn.execute("SELECT project_id FROM session_v2 WHERE id = ?", (args.session,)).fetchone()
            if row is None:
                die(f"unknown session: {args.session}")
            if row[0] != project:
                die(f"session {args.session} belongs to another project — pass --project to opt in")
        # An empty project scope is an empty result set, not an error.
        count = conn.execute(
            "SELECT count(*) FROM session_message m"
            " JOIN session_v2 s ON s.id = m.session_id WHERE s.project_id = ?",
            (project,),
        ).fetchone()[0]
        if count == 0:
            emit(args, args.query, [], scope, {"cache": "empty", "documents": 0, "changed": 0, "build_ms": 0})
            return
        index, stats = load_project_index(conn, project, cache_root, refresh=not args.no_refresh)
        doc_ids = session_doc_ids(conn, args.session) if args.session else None
        if args.session and not doc_ids:
            # Guard: an empty filter_paths list would silently degrade to an
            # unfiltered search of the whole project.
            emit(args, args.query, [], scope, stats)
            return
    results = index.search(args.query, top_k=args.top_k, filter_paths=doc_ids if doc_ids else None)
    emit(args, args.query, result_rows(conn, results, scope), scope, stats)


def run_expand(args) -> None:
    """The read-companion to a search hit: one message, rendered in full."""
    target = args.message.removesuffix(".md")
    if "/" in target:
        target = target.rsplit("/", 1)[-1]
    conn = open_db(Path(args.db).expanduser())
    row = conn.execute("SELECT type, data FROM session_message WHERE id = ?", (target,)).fetchone()
    if row is None:
        die(f"no message found for: {args.message}")
    try:
        data = json.loads(row[1])
    except ValueError:
        data = None
    text = render_block(target, row[0], data) if isinstance(data, dict) else f"## {target} [raw]\n{row[1]}"
    show_text(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="history-search",
        description="Semantic search over OpenCode conversation history (the DB as corpus).",
    )
    parser.add_argument("query", nargs="?", help="natural-language or code query over conversation history")
    parser.add_argument("-k", "--top-k", type=int, default=5, help="number of results (default 5)")
    parser.add_argument("-j", "--json", action="store_true", help="machine-readable JSON (used by the history plugin)")
    parser.add_argument("-m", "--message", help="expand one message by id (msg_…) instead of searching")
    parser.add_argument(
        "--project",
        help="project ID, or 'all' for cross-project search (default: the project owning the cwd, else all projects)",
    )
    parser.add_argument("--session", help="restrict retrieval to one session (ses_…)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"opencode database (default: {DEFAULT_DB})")
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT), help=f"index cache root (default: {DEFAULT_CACHE_ROOT})")
    parser.add_argument("--no-refresh", action="store_true", help="search the persisted index as-is")
    parser.add_argument("--prewarm", action="store_true", help="refresh the index without searching")
    parser.add_argument("--where", action="store_true", help="print the resolved index directory and exit")
    args = parser.parse_args()

    if not args.query and not args.message and not args.prewarm and not args.where:
        parser.error('query required — try: history-search "where did we decide Y"')

    cache_root = Path(args.cache_root).expanduser().resolve()
    # All semble index persistence lives under one root we own (and the
    # opencode shell sandbox can write) instead of semble's platform default.
    os.environ["SEMBLE_CACHE_LOCATION"] = str(cache_root)

    if args.where:
        conn = open_db(Path(args.db).expanduser())
        project = args.project or resolve_project(conn)
        if project is None:
            print(cache_root)
        else:
            from semble import ContentType
            from semble.cache import find_index_from_cache_folder

            print(find_index_from_cache_folder(project_key(project), (ContentType.DOCS,)))
        return

    if args.message:
        run_expand(args)
        return

    if args.prewarm:
        conn = open_db(Path(args.db).expanduser())
        project = args.project or resolve_project(conn)
        if project is None:
            die("no project for this directory — pass --project")
        _, stats = load_project_index(conn, project, cache_root)
        print(
            f"index: {stats['documents']} documents, {stats['changed']} changed,"
            f" {stats['build_ms']}ms ({stats['cache']})"
        )
        return

    run_search(args, cache_root)


if __name__ == "__main__":
    main()
