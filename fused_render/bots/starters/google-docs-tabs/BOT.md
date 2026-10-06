# Google Docs Tabs

Run `docs.py` with `main(action=..., ...)`. Every action returns a JSON dict;
failures come back as `{"error": "..."}` (never an exception) so check that
key first. `doc` accepts a full Google Docs URL, a bare document id, **or the
name of a saved document**.

The app keeps a **saved documents library** (`.fused/data/docs.json`): every
doc that is used gets saved automatically under its title, and the user can
rename entries to friendly names in the page. When the user says "the roadmap
doc", call `list_docs` (or `status`) to see the names, then pass that name as
`doc`. Name matching is case-insensitive, exact first, then unique substring.

Requires a one-time setup done by the user in the page: paste a Google
service-account key JSON. `status` tells you whether that is done and gives
the service account's `email`. A 403/404 error on a doc means the doc is not
shared with that email; tell the user to share it as an Editor.

| action | params | returns |
| --- | --- | --- |
| `status` | — | `{connected, email, docs:[{id,name,title,url,last_used}]}` |
| `list_docs` | — | `{count, docs:[{id,name,title,url,added_at,last_used}]}` |
| `add_doc` | `doc` (URL or id), `title` (friendly name, optional) | `{ok, doc, tabs}` (verifies the doc opens) |
| `rename_doc` | `doc` (name/id/URL), `title` (new name) | `{ok, doc}` |
| `remove_doc` | `doc` (name/id/URL) | `{ok, removed}` (library only; the Google Doc is untouched) |
| `list_tabs` | `doc` | `{doc_id, title, tabs:[{id,title,index,depth,parent,preview}]}` |
| `read_tab` | `doc`, `tab` (id or title) | `{tab:{id,title}, text}` |
| `create_tab` | `doc`, `title`, `content` (optional), `parent` (optional tab id) | `{ok, tab:{id,title}, inserted}` |
| `append` | `doc`, `tab` (id or title), `content` | `{ok, tab:{id,title}, inserted}` |
| `write_tab` | `doc`, `tab` (id or title), `content` | replaces the whole tab: `{ok, tab:{id,title}, inserted}` |
| `list_comments` | `doc`, `resolved` (bool, include resolved) | `{count, comments:[{id,content,author,created,resolved,quoted,replies:[…]}]}` |
| `add_comment` | `doc`, `content` | `{ok, comment:{id,…}}` (unanchored, shows in the doc's comment list) |
| `reply_comment` | `doc`, `comment` (id), `content` | `{ok, comment:{…, replies}}` |
| `resolve_comment` / `reopen_comment` | `doc`, `comment` (id), `content` (optional note) | `{ok, comment}` |
| `delete_comment` | `doc`, `comment` (id) | `{ok, deleted}` |
| `command` | `doc`, `command` (plain English) | `{ok, interpreted:{action,title,content}, result}` |

Comments go through the Google Drive API, so the service account's Cloud
project must have the **Drive API** enabled as well as the Docs API; the
error message says so if it is not. The same actions are published as MCP
tools in `mcp.toml` (one `docs_*` tool per action).

`content` is plain text with light markdown: `# ` / `## ` / `### ` become
headings, lines starting with `- ` become bullets, `1. ` become a numbered
list. Everything else is inserted verbatim, one paragraph per line.

Example: `main(action="create_tab", doc="https://docs.google.com/document/d/<id>/edit", title="Meeting notes", content="# Agenda\n- Roadmap\n- Hiring")`.
With a saved document: `main(action="append", doc="Team roadmap", tab="Q4", content="- Ship v2")`.
