---
name: fused-render-tasks
description: Use when a page lists, creates, follows up, cancels or observes the app's Claude tasks — fused.tasks — or needs a live task board; not for fused.trackJob progress rows.
---

# fused.tasks

Task = ONE Claude Code session. Same rows the Tasks page shows, same `/api/tasks` routes — page just drives them. Local only.

## Which tool

| Need | Use |
|---|---|
| Human should chat with Claude about this page now | `_fusedAskClaude` — opens sidebar, human drives. |
| Page starts Claude work itself, headless, and tracks it | `fused.tasks.create` → handle. No sidebar opens. |
| Page's OWN long work (download, build) needs a progress row | `fused.trackJob` → `fused-render-jobs`. Not a task. |

## Signatures

```js
fused.tasks.list(opts?)            // -> Task[]   opts {scope?: "app"|"all", status?: Status[], archived?: boolean}
fused.tasks.get(key, {scope?})     // -> Task | null; scope "app" (default) → null for a key outside this app
fused.tasks.create({prompt, target?, title?, model?, effort?, permissionMode?, due?})  // -> TaskHandle
fused.tasks.send(key, text, opts?) // -> {queued: boolean}
fused.tasks.cancel(key)            // interrupt, then kill
fused.tasks.archive(key) / .unarchive(key) / .delete(key)
fused.tasks.markRead(key, messageId?)
fused.tasks.messages(key)          // -> TaskMessage[]
fused.tasks.transcript(key, {scope?, ...}) // -> HistoryEntry[]
fused.tasks.settings(key, {model?, effort?}, {scope?})
fused.tasks.watch(fn, opts?)       // -> unsubscribe(); fn(rows, change)
fused.tasks.ui({view?, task?, scope?}) // -> Promise<url> for an <iframe>: the shell's Tasks UI

// TaskHandle
h.key        // getter: "pending:<entry>" first, then the session id
h.entryId
h.get() / h.send(text) / h.cancel() / h.archive()
h.watch(fn)  // -> unsubscribe()
h.done       // Promise<Task>, settles when the task reaches done/archived AND it was seen running first
             // (or a quiet row stays quiet 15 s); a send() re-arms it, so a follow-up never resolves on the stale row
```

Every call returns a Promise unless noted. `Status` = server's seven, derived per listing, never stored:
`upcoming`, `queued`, `in_progress`, `needs_attention`, `blocked`, `done`, `archived`.
`queued` only appears with the project-queue flag on (another task holds the same folder).

## Keys rekey — keep the handle

`create` has no session yet. Key starts `pending:<entry>`; once Claude starts, the row rekeys to the real session id (joined via `entry_id`). Handle follows: `h.key` getter is always current. Saving the `h.key` string at create time = stale key after rekey → `get` returns null, `send`/`cancel` miss. Keep the handle, or re-read `h.key` at each use. Across reloads: store `h.entryId`, find the row by `entry_id` in `list()`.

## Scope

`list`/`watch` default `scope: "app"` — only tasks under THIS page's app folder, filtered server-side. An app's board sees that app only. `scope: "all"` = every task on the machine; only for machine-wide views.

## permissionMode

Default `"default"` — NOT the scheduler's `"auto"`. Claude asks before risky tools; each ask parks the task as `needs_attention`, answered by the human in the Tasks page, not by the page. `"auto"` = no asks; `"plan"` = plan mode, no edits. Pick `"auto"` only when the prompt is safe unattended. A follow-up `send` on a task whose session host has gone idle resumes under the mode the task was created with; a task the page did not create (a chat-born session) resumes under `"default"`, never `"auto"` — a page cannot lift a user's conversation to unattended mode by writing to it.

## Embedding the shell's own Tasks UI

Don't rebuild the task list. `fused.tasks.ui()` returns a same-origin URL of the shell's Tasks page in chrome-less mode (no sidebar, docks or breadcrumb), scoped to this app's tasks by default:

```js
const frame = document.querySelector("iframe#tasks");
frame.src = await fused.tasks.ui({ view: "board" });          // list | board | cards | calendar
frame.src = await fused.tasks.ui({ task: h.key });            // one task's detail + chat beside the list
frame.src = await fused.tasks.ui({ scope: "all" });           // every task on the machine
```

Async because the SERVER builds the URL — it resolves your app folder from the page header, so the page never guesses it. `task` takes any listing key (a `pending:<entry>` key works before the session exists). The frame inherits your page's theme through the shell's ancestor climb; no theme param. Needs the `task_peek_enabled` pref (default on) for `task` to open the side panel.

## Hosted gate

Present on exported pages and THROWS (like `writeFile`). Exporter does NOT block it (dotted call slips past its `fused.<name>(` check). Obligation is yours:

```js
if (fused.env === "local") drawBoard(); else hideBoard();
```

## Minimal task board

```html
<ul id="rows"></ul>
<form id="f"><input name="p" placeholder="Ask Claude…"><button>Run</button></form>
<script>
if (fused.env === "local") {
  const ul = document.getElementById("rows");
  const draw = rows => ul.replaceChildren(...rows.map(t => {
    const li = document.createElement("li");
    li.textContent = `${t.status} — ${t.title}`;
    return li;
  }));
  fused.tasks.list().then(draw);
  const stop = fused.tasks.watch(rows => draw(rows));   // one shared long-poll per document
  addEventListener("pagehide", stop);

  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    const h = await fused.tasks.create({ prompt: e.target.p.value });
    e.target.reset();
    const t = await h.done;                  // follows the rekey
    await fused.tasks.markRead(h.key);       // h.key, NOT a saved string
    console.log("finished", t.title);
  };
}
</script>
```

## Pitfalls

- **Status derived, can flicker.** Within ~15 s of a `send` a row can read `done` then `in_progress` then settle as messages land. Don't fire one-shot side effects on a single transition; await `h.done` or require the status stable across two changes.
- **Pending key before session.** `pending:<entry>` rows exist before Claude starts. Don't treat that key as an error or persist it.
- **Archived excluded by default.** `list()`/`watch` hide archived rows; pass `archived: true` to include them.
- **`send` may queue.** Project-queue flag on + folder busy → `{queued: true}`: accepted, runs later. Not a failure.
- **`cancel` ≠ archive.** Cancel stops the process; the row stays. Archive files it away.
- **No erase.** Deleting the transcript is Tasks-page only. Also absent: queue doors (admit/skip/force/decide) and the running/idle marks.
- **One long-poll per document.** Many `watch` calls share it; always call the returned unsubscribe.
- Writes carry the `X-Fused` header — the runtime sends it. Don't hand-roll `fetch("/api/tasks/...")`.
