// history — conversation-history search for OpenCode v2.
//
// Two tools (registered Code Mode catalog-only via `codemode: true`, so they
// cost ~zero context in Code Mode sessions):
//
//   history_search  retrieval over full conversation transcripts — what
//                   sessions discussed, decided, or tried. Scope: 'all' (the
//                   whole project corpus — the asker's own history included;
//                   this is memory, not just team curiosity) or a session ID
//                   for exactly that conversation. `project` opts into
//                   another project's history.
//   history_expand  one message by ID, fetched read-only from the opencode
//                   DB and returned as the raw record — the read-companion
//                   to a search hit (results carry messageID). No CLI, no
//                   index.
// Powered by the semble fork (https://github.com/jabr/semble, branch
// source-abstraction) reading opencode's SQLite DB directly as a document
// source — the DB IS the corpus, so there is no sync/materialization layer:
// no watermarks, no session caps, nothing to prune; messages deleted from
// the DB fall out of the index on the next refresh. Everything lives in
// this package directory: search.py (the CLI) and history-search (the venv
// launcher). The terminal twin is the same CLI — see the README for the
// optional ~/.local/bin/history-search symlink.
//
// Scoping policy is enforced in RETRIEVAL, not by post-filtering: each
// project has its own index (only that project's messages are ever
// indexed), and a session scope narrows retrieval via semble's pre-ranking
// selector (filter_paths) before top-k. This plugin only decides the scope
// and shapes the output; search.py owns everything else.
// ---------------------------------------------------------------------------
// Pre-rewrite findings (documented 2026-11-07, against the file-sync
// implementation this replaces — kept so the old behaviour is not
// re-derived; its line references died with it). Most items are obsolete
// by design under db-as-source; (1) is the one carried forward — now
// enforced for real, since the per-project index and pre-ranking session
// selector make scope a retrieval filter, not a sync filter.
//
// 1. `scope` was a sync filter, not a retrieval filter: `input.scope` only
//    selected which sessions the sync refreshed; every branch still searched
//    the whole corpus/<projectID> dir. Live check: scope=<sessionID> and
//    scope="all" returned the same 7 sessionIDs / 20 hits, with zero hits
//    from the requested session. Sessions past the sync cap also remained
//    on disk and stayed searchable.
//
// 2. Narrowing the search root to corpusDir/<sessionID> fixed (1) but cost
//    three things (measured on semble 0.7.0): `file_path` became relative to
//    that root so the sessionID back-reference came back empty; semble keyed
//    its cache on the root path so a new root built a new full index and
//    duplicated the parent's chunks/embeddings (~1 KB/chunk for vectors.npy);
//    and an empty session dir made semble exit 1 with its traceback on
//    STDOUT so the stderr-tail error came back blank. No path-filter flag
//    existed, and post-filtering after `-k` truncation could yield 0 target
//    hits — the pre-ranking selector in the fork's search() is what finally
//    solved this.
//
// 3. Two semble caches were deliberate, not a bug: this plugin set
//    SEMBLE_CACHE_LOCATION next to the corpus, while other consumers (the
//    MCP server in opencode.json) used semble's platform default
//    (~/Library/Caches/semble). Disjoint corpora and index kinds — no
//    duplicated work. However ~/.local/share/opencode was read-only in the
//    opencode shell sandbox, so searches run through the sandboxed shell
//    could not refresh the corpus or write the index (soft-failed
//    silently). The rewrite moves the index store to
//    ~/.cache/opencode/history-search, which the sandbox can write, so the
//    terminal path refreshes too.
//
// 4. semble 0.7.0 printed one JSON line {"query","results":[...]} with
//    score/file_path/start_line/end_line/content; the "one object per result
//    set" parsing assumption from 0.6.1 was stale. The rewrite replaces the
//    CLI wrapper entirely (library, JSON via argparse), so this is moot.
// ---------------------------------------------------------------------------
import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { DatabaseSync } from "node:sqlite"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"

const MAX_TEXT = 500
const DEFAULT_LIMIT = 20
const LAUNCHER = path.join(path.dirname(fileURLToPath(import.meta.url)), "history-search")
// Same default as search.py's DEFAULT_DB.
const DB_PATH = path.join(os.homedir(), ".local", "share", "opencode", "opencode.db")

// Keep tool output compact — snippets are truncated per hit.
function truncate(text: string, max = MAX_TEXT): string {
  const flat = text.replace(/\s+/g, " ").trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}

/** Run the CLI launcher, surfacing launcher-level failures with hints. */
function runLauncher(args: string[], label: string): string {
  const run = spawnSync(LAUNCHER, args, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 })
  if (run.error) {
    const errno = run.error as NodeJS.ErrnoException
    const hint =
      errno.code === "ENOENT" ? " — the launcher should live beside this file (history-search, next to index.ts)" : ""
    throw new Error(`${label}: ${run.error.message}${hint}`)
  }
  if (run.status !== 0) {
    // The last stderr lines usually name the real cause (missing DB,
    // scope violation, offline uv …).
    const tail = run.stderr.trim().split("\n").slice(-3).join(" ")
    throw new Error(`${label}: ${tail || `exited with status ${run.status}`}`)
  }
  return run.stdout
}

/** One search result row, as emitted by search.py --json. Only IDs that vary
 * per hit appear: projectID in cross-project search, sessionID when not
 * session-scoped. Enrichment (owning directory, session title, message
 * type/time) is omitted when unknown. */
type ResultRow = {
  projectID?: string
  project?: string
  sessionID?: string
  messageID: string
  title?: string
  type?: string
  time?: string
  score: number
  snippet: string
}

export default Plugin.define({
  id: "history",
  async setup(ctx) {
    const projectID = ctx.location.project.id

    await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "history",
        description: "Conversation-history memory: search what was discussed, decided, or tried",
      })

      editor.add({
        name: "search",
        description:
          "Search full conversation history across sessions: what was discussed, decided, or tried — " +
          "use when asked why something was done or what past conversations concluded. " +
          "Indexes whole transcripts (reasoning, tool calls, and results included); " +
          "results carry sessionID + messageID + snippet per hit, enriched with the session title, " +
          "message type, timestamp, and (cross-project) the owning directory. " +
          "Expand a hit in full with history.expand. " +
          "Defaults to all sessions in the project; `scope` narrows to one conversation.",
        input: {
          type: "object",
          properties: {
            query: {
              type: "string",
              description: "natural-language or code query over conversation history (past discussions, decisions, attempts).",
            },
            scope: {
              type: "string",
              description: "'all' (default — the whole project corpus) or a session ID (ses_…) for exactly that conversation.",
            },
            project: {
              type: "string",
              description: "project ID to search another project's history, or 'all' for cross-project search (opt-in; defaults to this project).",
            },
            limit: { type: "integer", description: "max results (default 20)." },
          },
          required: ["query"],
          additionalProperties: false,
        },
        // Declared so Code Mode types the tool's return as this object.
        // Undeclared outputs surface as `Promise<string | null>` — the raw
        // content string — forcing every caller to JSON.parse (and to
        // remember to). Raw JSON Schema output is not strictly validated,
        // only required to be JSON-serializable, so this documents the shape
        // without risking decode errors.
        output: {
          type: "object",
          properties: {
            query: { type: "string" },
            scope: {
              type: "object",
              properties: {
                project: { type: ["string", "null"] },
                session: { type: ["string", "null"] },
              },
            },
            results: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  projectID: { type: "string" },
                  project: { type: "string" },
                  sessionID: { type: "string" },
                  messageID: { type: "string" },
                  title: { type: "string" },
                  type: { type: "string" },
                  time: { type: "string" },
                  score: { type: "number" },
                  snippet: { type: "string" },
                },
              },
            },
            index: { type: "object" },
          },
        },
        options: { namespace: "history", permission: "history_search", codemode: true },
        execute: async (raw) => {
          const input = raw as {
            query: string
            scope?: string
            project?: string
            limit?: number
          }
          if (!input.query) throw new Error("history search: query is required")
          // Retrieval is project-trapped like every read; `project` opts into
          // another project's history. search.py enforces this in the index
          // itself — only the scoped project's messages are ever searched.
          const scope = input.project ?? projectID
          const args = ["--json", "-k", String(input.limit ?? DEFAULT_LIMIT), "--project", scope]
          if (input.scope && input.scope !== "all") args.push("--session", input.scope)
          args.push(input.query)
          const out = JSON.parse(runLauncher(args, "history search")) as {
            scope?: { project?: string | null; session?: string | null }
            results?: ResultRow[]
            index?: Record<string, unknown>
          }
          const results = (out.results ?? []).map((row) => ({ ...row, snippet: truncate(row.snippet) }))
          return {
            output: { query: input.query, scope: out.scope, results, index: out.index },
          }
        },
      })

      editor.add({
        name: "expand",
        description:
          "Expand one conversation-history message in full by ID (msg_…): the raw message record — " +
          "messageID, sessionID, type, seq, time, and the complete persisted data JSON (parsed), with text, " +
          "reasoning, and tool calls and their results. " +
          "The read-companion to a search hit (search results carry messageID).",
        input: {
          type: "object",
          properties: {
            message: {
              type: "string",
              description: "message ID (msg_…, as carried by search results; a path suffix like ses_…/msg_….md is accepted).",
            },
          },
          required: ["message"],
          additionalProperties: false,
        },
        // Same JSON Schema output discipline as search: typed object in Code
        // Mode, shape documented, not strictly validated. `data` is the
        // message's persisted JSON — any shape, so its schema is open.
        output: {
          type: "object",
          properties: {
            messageID: { type: "string" },
            sessionID: { type: "string" },
            type: { type: "string" },
            seq: { type: "integer" },
            time: {
              type: "object",
              properties: { created: { type: "integer" }, updated: { type: "integer" } },
            },
            data: { description: "the message's persisted data JSON, parsed as-is" },
          },
          required: ["messageID", "sessionID", "type", "seq", "time", "data"],
        },
        options: { namespace: "history", permission: "history_expand", codemode: true },
        execute: async (raw) => {
          const input = raw as { message: string }
          if (!input.message) throw new Error("history expand: message ID is required")
          // Same normalization as the CLI twin: a ses_…/msg_….md path suffix is accepted.
          const target = path.basename(input.message.replace(/\.md$/, ""))
          if (!existsSync(DB_PATH)) throw new Error(`history expand: database not found at ${DB_PATH}`)
          const db = new DatabaseSync(DB_PATH, { readOnly: true })
          let row: {
            id: string
            session_id: string
            type: string
            seq: number
            time_created: number
            time_updated: number
            data: string
          }
          try {
            const found = db
              .prepare(
                "SELECT id, session_id, type, seq, time_created, time_updated, data FROM session_message WHERE id = ?",
              )
              .get(target)
            if (!found) throw new Error(`history expand: no message found for ${input.message}`)
            row = found as typeof row
          } finally {
            db.close()
          }
          // `data` is the message's persisted JSON; malformed rows keep the
          // raw string rather than failing the read.
          let data: unknown = row.data
          try {
            data = JSON.parse(row.data)
          } catch {}
          return {
            output: {
              messageID: row.id,
              sessionID: row.session_id,
              type: row.type,
              seq: row.seq,
              time: { created: row.time_created, updated: row.time_updated },
              data,
            },
          }
        },
      })
    })

    // Usage guidance, injected system-side on every model call (same
    // mechanism as the team plugin). This keeps the plugin self-contained —
    // no AGENTS.md section to install or drift from the tool's behavior.
    // The Code Mode invocation is named because catalog-only tools never
    // appear in the direct tool list. The terminal twin is mentioned only
    // when it is actually on PATH — advertising a command the user never
    // symlinked would just produce ENOENT failures — and agents have Code
    // Mode for transforms regardless; the twin matters for the rare
    // shell-pipeline case.
    const twinOnPath =
      spawnSync("sh", ["-c", "command -v history-search >/dev/null 2>&1"]).status === 0
    await ctx.session.hook("context", (event) => {
      event.system.push({
        type: "text",
        text:
          "Conversation-history memory is active (history plugin): past sessions' full transcripts are searchable. " +
          "For \"why did we do X\" or what was previously discussed, decided, or tried, query it rather than " +
          "re-deriving the answer from current code — via Code Mode, tools.history.search inside execute, and " +
          "tools.history.expand({ message }) for one hit's full content. " +
          "It covers this project's sessions by default, the asker's own history included." +
          (twinOnPath
            ? " A terminal twin, `history-search`, is on PATH for shell pipelines."
            : ""),
      })
    })
  },
})
