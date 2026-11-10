# History

Conversation-history memory for [OpenCode](https://opencode.ai) V2. One plugin
package gives every session a `history_search` tool — semantic retrieval over
full conversation transcripts (reasoning, tool calls, and results included) —
plus the `history-search` terminal CLI for the same corpus in shell pipelines.

The opencode SQLite DB **is** the corpus — no files are synced or
materialized — indexed by the semble fork ([jabr/semble][fork], branch
`source-abstraction`) through its `Source` API.

[fork]: https://github.com/jabr/semble

## Install

Prerequisites are listed under [Requirements](#requirements) — the only hard
one is [uv](https://docs.astral.sh/uv/).

Install from GitHub (this package is **not** published to npm):

```sh
opencode plugin add github:jabr/opencode-history-plugin
```

Or reference it directly in `opencode.json` — as a Git package spec or a local
path:

```jsonc
{
  "plugins": [
    "github:jabr/opencode-history-plugin",
    // or: "../libs/opencode-history-plugin"
  ]
}
```

The terminal twin is the same CLI; a symlink makes it available on PATH:

```sh
ln -s "$PWD/history-search" ~/.local/bin/history-search
```

## Tool

### `history_search`

Search full conversation history across sessions: what was discussed,
decided, or tried. Use it when asked *why* something was done or what past
conversations concluded, rather than re-deriving the answer from current
code. The asker's own history is included — this is memory, not just team
curiosity.

| Parameter | Meaning |
|---|---|
| `query` | natural-language or code query (required) |
| `scope` | `"all"` (default — the whole project corpus) or a session ID (`ses_…`) for exactly that conversation |
| `project` | project ID to search another project's history, or `"all"` for cross-project search (opt-in; defaults to this project) |
| `limit` | max results (default 20) |

Results carry `messageID` + `snippet` per hit (and `sessionID`, unless
session-scoped). A context hook injected on every model call tells agents the
tool exists and when to reach for it, so no AGENTS.md section needs
installing.

The tool is registered `codemode: true` (catalog-only in Code Mode sessions),
so it costs ~zero context there and is invoked as `tools.history.search`
inside `execute`.

## The `history-search` CLI

One CLI, two faces: markdown-through-mdcat for humans (the terminal twin of
the tool), `--json` for scripts (what the plugin uses).

```sh
history-search "where did we decide Y"       # semantic search, human output
history-search -k 10 -j "auth refactor"      # 10 results as JSON
history-search -m msg_…                      # expand one message in full
history-search --prewarm                     # build/refresh the index, no search
history-search --where                       # print the resolved index directory
```

`--project <id|all>` and `--session ses_…` mirror the tool's scoping;
`--no-refresh` searches the persisted index as-is. The `messageID` in either
output mode is the copyable handle for `-m`.

## Layout

| file | role |
|---|---|
| `index.ts` | the plugin: scope policy, spawns the launcher, snippet truncation |
| `search.py` | the CLI: DB source, index refresh, search, human + `--json` faces |
| `history-search` | bash launcher: builds/execs the persistent venv |

## Indexes and refresh semantics

One persisted index **per project**, at
`~/.cache/opencode/history-search/<sha256>/index-docs/` (`--where` prints the
resolved path). Documents are `<sessionID>/<messageID>.md` — one per message,
versioned by `time_updated` — so a chunk never spans messages and a hit maps
back to its message by ID. The `.md` suffix is semble-internal (it selects
markdown-aware chunking); it never appears in output.

What refreshes when:

| scope | refresh? |
|---|---|
| project (default) | **yes** — whole project index, incrementally |
| session (`--session`) | **yes** — identical refresh; only retrieval narrows |
| cross-project (`--project all`, or unresolvable cwd) | **no** — merged from persisted indexes as-is |
| `--no-refresh` | **no** — search the persisted index as-is |

Incremental means: one SQL scan compares every message's `time_updated` against
the index manifest, only changed messages are re-read and re-embedded, and the
disk save is skipped entirely when nothing changed. Messages deleted from the
DB drop out of the index on the next refresh. A project's index is refreshed by
any project- or session-scoped search in it — cross-project searches are served
best-effort from whatever was last indexed.

## Scoping

Enforced in retrieval, never by post-filtering:

- **Project** — the index only ever contains that project's messages.
- **Session** — the same index, with semble's pre-ranking selector
  (`filter_paths`) restricting retrieval to that session's documents *before*
  top-k, so narrowing is exact (no truncation artifacts).
- **Cross-project** — opt-in via `--project all` (or an unresolvable cwd);
  `SembleIndex.merge` over every project's persisted index, rows carry
  `projectID`/`sessionID` since they vary.

JSON rows carry only the IDs that vary (`scope` above them states the rest):
project scope → `sessionID`, `messageID`, `score`, `snippet`; session scope →
`messageID`, `score`, `snippet`; merged → both IDs. `"scope": {"project": null}`
marks cross-project search.

## Project resolution (cwd → project, most authoritative first)

1. `worktree` — the current registry; opencode's own resolve persists here.
   Newest `time_created` wins for directories that span projects
   (e.g. `global` first, then a proper project once registered).
2. `project_directory` — deprecated in the v2 source with no remaining
   readers or writers; kept only for legacy rows predating `worktree`.
3. `session_v2.directory` — the only complete record; resolves through the
   most recent session run in the longest matching ancestor directory.

The plugin never resolves — it passes `ctx.location.project.id` and the
session/project opt-ins explicitly.

## Permissions

The tool has its own permission key — `history_search` — so config can gate
it:

```jsonc
{
  "permission": {
    "history_search": "deny", // e.g. keep subagents from reading history
  }
}
```

## Requirements

- OpenCode V2 (the plugin API).
- [uv](https://docs.astral.sh/uv/) — builds the persistent venv on first use
  (see [Ops notes](#ops-notes)); network access at build time to fetch the
  semble fork from GitHub, soft-failing to the existing venv when offline.
- [mdcat](https://github.com/swagDevOps/mdcat) (optional) — markdown
  rendering for the CLI's human face; raw text without it.

## Ops notes

- **Runtime**: a persistent venv at `~/.cache/opencode/history-search/venv`
  (symlink → a timestamped build, swapped atomically), holding the fork pinned
  by branch (not commit). uv only runs at build time — when the venv is
  missing, forced with `HISTORY_SEARCH_REFRESH=1`, or at most daily (branch
  re-resolve, soft-failing to the existing venv when offline). Query-time runs
  exec the venv python directly: `uv run --with` was rejected because it
  resolves an ephemeral environment on every run, those environments are
  cache-disposable (GC or an interpreter change reinstalls every dependency),
  and interpreter discovery honors `.python-version` in the cwd — so a new
  project directory could fork a whole second environment. The venv
  interpreter floats (`--python 3`: newest managed uv python; an explicit
  request ignores `.python-version`): installing a newer managed Python
  migrates the venv at the next rebuild, and a new CPython whose deps lack
  wheels fails the build — which soft-fails to the existing venv and retries
  next rebuild, so floating forward never breaks a running search.
  Concurrent builds serialize on a mkdir lock, and old builds linger briefly
  for in-flight processes before being removed.
- **Cold build**: first search in a project embeds all its messages (seconds,
  once). `--prewarm` warms deliberately.
- **Concurrency**: refresh + save run under an `flock` per project; a torn
  read self-heals via semble's consistency validation → cold rebuild.
- **Sandbox**: `~/.cache/` is writable from the opencode shell sandbox, so
  terminal searches refresh too. mdcat must be able to read its config
  (`~/.config/mdcat`); `show_text` falls back to raw output if mdcat fails —
  it canaries the render because mdcat exits 0 even when broken.
- **DB access**: read-only (`mode=ro`) against the live WAL database; safe
  alongside the running server.
- **Disk**: every message ever, embedded — order a few hundred MB at ~77k
  messages. Unbounded by design (deletions in the DB do propagate); a
  scan-time bound is the prune lever if it ever matters.

## License

[MIT](LICENSE.txt) — Copyright (c) 2026 Justin Bradford.
