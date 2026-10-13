---
name: fused-render-bots
description: Use when a page lists the Bots on this Mac, sends one a message or an image, follows that task to its result, or needs to know why a send was refused — fused.bots. Not for fused.tasks (Claude Code sessions) or fused.ai (one model call).
---

# fused.bots

A bot = one browser the local server drives (Chrome over CDP) with its own logins, memory and chat, the rows the Bots page shows. `fused.bots` lets an app page see them and give one a task; the bot's own chat shows the task tagged "from <app>" and every approval card still lands with the human there. Local only.

## Which tool

| Need | Use |
|---|---|
| One model call, no browser, no memory | `fused.ai` → `fused-render-ai` |
| Claude Code on this folder's files | `fused.tasks` → `fused-render-tasks` |
| A bot that browses the web with the user's logins, or Super Bot with Mac access | `fused.bots` (this skill) |

## Signatures

```js
fused.bots.list()                      // -> Bot[]      every bot, Super Bot included (kind "super")
fused.bots.get(idOrName)               // -> Bot | null  id, else exact name (case-insensitive)
fused.bots.send(idOrName, text, opts?) // -> BotTurn     opts {files?: [File|Blob|{name, blob}]}
fused.bots.watch(fn)                   // -> unsubscribe fn(bots) every 1.5 s, one shared poll

// Bot
{ id, name, kind: "bot"|"super", preset, description,        // description: first 240 chars of its instructions
  status: "idle"|"running"|"waiting"|"paused"|"error", busy,  // busy: a task is running or waiting, any origin
  task, note, step, waitingOn: "question"|"approval"|"login"|null,
  model, effort, face: {shape, color, icon}, hidden, pinned, updated }

// BotTurn — one task this page started on one bot
t.bot, t.addr, t.vision   // vision: the engine can see the images sent (false for a local model)
t.state                   // "working" | "blocked" | "done" | "failed" | "cancelled"
t.blocked                 // {kind: "question"|"approval"|"login", text, seq, options?} while blocked
t.events                  // this turn's events so far (thought, action, note, question, approval, done, error…)
t.watch(fn)               // fn(event, turn) per new event -> unsubscribe
t.send(text, {files?})    // follow-up while live: an instruction, or the answer to the bot's `ask`
t.stop()                  // stop THIS task only
t.done                    // Promise<{state, text, summary, seq, artifacts, events}>, never rejects
```

Every verb returns a Promise; a failure rejects with an Error carrying the server's sentence plus `.status` and `.error`. A refused send is 409 with `.reason`: `busy` (a task the app did not start is running), `headed` (the browser is popped out as a real window), `control` (the user is driving it), `ended` (a `t.send` after the turn settled: an addr is never reused for a new task). Not a queue: show the reason, and `watch` tells you when the bot is idle again.

## Images and files

`files` go to the bot's `files/` folder exactly as a composer attachment does (8 MB each; the message gets the same "Attached (in your FILES folder…)" line). An image (png, jpeg, gif, webp) is also pushed into the model turn beside the text, scaled client-side to the API's 1568 px long edge, so "what does this error say?" with a screenshot just works. A follow-up `t.send(text, {files})` does the same mid-task. `t.vision === false` means the bot runs a text-only engine (a local model): the file is saved and named, but the model cannot look.

## Super Bot

Listed and reachable. An app's task runs under the posture a phone text gets: every write, edit, shell command and build asks on a card in Super Bot's chat, whatever its access setting, and the turn reads `blocked {kind: "approval"}` until the user answers at the Mac. A page is not the user typing.

## What a message can and cannot answer

An app message is marked as from outside: it is an instruction to the running task, or the answer to the bot's own `ask` (render `t.blocked.options` and send the pick), and **never** the verdict of an approval card or an app offer. Those are the user's, in the Bots page.

## Hosted gate

Present on exported pages and THROWS (like `fused.tasks`). The exporter does not block it (dotted call), so:

```js
if (fused.env === "local") drawBots(); else hideBots();
```

## Minimal use

```html
<select id="bot"></select>
<form id="f"><input name="p" placeholder="Ask the bot…"><input type="file" name="shot" accept="image/*"><button>Send</button></form>
<pre id="out"></pre>
<script>
if (fused.env === "local") {
  const sel = document.getElementById("bot"), out = document.getElementById("out");
  fused.bots.list().then(bots => sel.replaceChildren(...bots.map(b => new Option(`${b.name}${b.busy ? " (busy)" : ""}`, b.id))));
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    const files = [...e.target.shot.files];
    let t;
    try { t = await fused.bots.send(sel.value, e.target.p.value, { files }); }
    catch (err) { out.textContent = err.reason === "busy" ? `Busy: ${err.task}` : err.message; return; }
    t.watch(ev => { if (ev.role === "note" || ev.role === "action") out.textContent = ev.text; });
    const r = await t.done;
    out.textContent = r.state === "done" ? r.text : `${r.state}: ${r.text}`;
  };
}
</script>
```

## Pitfalls

- **Blocked is not finished.** A `question` or `approval` parks the turn; `t.done` keeps waiting. Show `t.blocked.text` and, for a question, let your user answer with `t.send`.
- **One task at a time per bot.** A second `send` to a bot while your own turn is live is a follow-up to it (same `addr`), not a new task.
- **Only your own turns.** The page reads events of tasks it started, never the user's own conversation with the bot or another app's turns.
- **Thumbnails.** An `action` event's `thumbUrl` is the step screenshot (`/api/bots/<id>/steps/<seq>.jpg`); cache only, may 404 later.
- **No pause, resume, settings, delete, tabs, take-over.** Those stay the Bots page's.
