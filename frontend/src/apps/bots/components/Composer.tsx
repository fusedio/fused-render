// The composer (OpenBot chat.js): Pause / Resume / Stop, pending attachment chips, the reply quote, attach + dictation
// buttons, the auto-growing textarea (Enter sends, Shift+Enter is a newline, Esc cancels the reply) and the send arrow.
// Files arrive by the attach (document + paperclip) button, paste (a pasted screenshot is renamed) or drop on the row; each goes to the bot's files
// folder as base64 (8 MB each) when you send, and the task names them.
// Slash commands (lib/commands): a lone "/" word opens the command popup above the row (↑↓ move, Tab completes, Enter /
// click runs, Esc closes); "/clear" runs Clear context instead of sending. Unknown "/foo" sends as plain text.
// Dictation: push-to-talk, not live: record the mic (/api/capture), transcribe with the local speech model
// (/api/ai/transcribe, a job), drop the text at the caret for review. Nothing is sent on its own.
import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent, type RefObject } from "react";
import { api, rawFileUrl, request, type Bot } from "../lib/api";
import { canClear, commandFor, matchCommands, slashQuery, type Command } from "../lib/commands";
import { jobNote } from "../lib/dictation";
import { statusLabel } from "../lib/derive";
import { fmtAgo, fmtBytes, fmtSecs } from "../lib/format";
import { act, errMsg, eventsOf, markSeen, setBase, setScrollToEnd, showBanner, useBotsSelector } from "../state/store";

export interface ReplyTo { seq: number; text: string }

export interface ComposerProps {
  /** The selected bot (undefined: everything disabled). */
  b: Bot | undefined;
  /** The message the next send answers; null when not replying. */
  reply: ReplyTo | null;
  setReply: (r: ReplyTo | null) => void;
  /** The thread, scrolled to its end after a send. */
  threadRef: RefObject<HTMLDivElement>;
}

export const ATTACH_MAX = 8 * 1024 * 1024;
const MIC_MAX = 120;  // seconds; the recording stops itself here and is still transcribed
const MIC_HINT = "Dictate: click to record, click again to stop. The text lands here for you to review before sending.";

const b64 = (f: File) => new Promise<string>((ok, no) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",")[1] || "");
  r.onerror = () => no(r.error);
  r.readAsDataURL(f);
});

/** Insert text at the caret (or over the selection) with a space on either side when needed, then re-fit (an input event). */
export function insertAtCaret(ta: HTMLTextAreaElement, text: string): void {
  const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? a;
  const before = ta.value.slice(0, a), after = ta.value.slice(b);
  const pad = before && !/\s$/.test(before) ? " " : "", tail = after && !/^\s/.test(after) ? " " : "";
  ta.value = before + pad + text + tail + after;
  const pos = (before + pad + text).length; ta.setSelectionRange(pos, pos);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

/** A pasted screenshot arrives as "image.png"; give it a unique, readable name. */
export const pastedName = (f: { name: string; type: string }, now = new Date()): string =>
  f.name && f.name !== "image.png" ? f.name
    : `pasted-${now.toISOString().slice(0, 19).replace(/[T:]/g, "-")}.${(f.type.split("/")[1] || "png").replace("jpeg", "jpg")}`;

// ---- dictation plumbing (fused.capture / fused.ai.transcribe / fused.watchJob, as fetches) ----
interface CaptureRec { id: string; jobId: string; path: string | null; seconds?: number; error?: string }
interface JobRow { id: string; state: string; message?: string; detail?: string; waiting_for?: string }
type TypedError = Error & { type?: string; jobId?: string };
const typed = (msg: string, type: string, jobId?: string): TypedError => Object.assign(new Error(msg), { type, jobId });
/** Where the transcription's progress sentence goes (the mic title and the placeholder while busy). */
type Note = (text: string) => void;

/** Poll /api/jobs (every 700 ms) until the job leaves running/waiting; null when it vanished (5 misses after a sighting) or `stopped()` says so.
 *  `tick` sees every sighting while it runs, so a long first-run phase can be narrated. */
async function watchJob(id: string, stopped: () => boolean = () => false, tick?: (rec: JobRow) => void): Promise<JobRow | null> {
  let seen = false, missing = 0;
  for (;;) {
    if (stopped()) return null;
    const rec = await request<{ jobs?: JobRow[] }>("GET", "/api/jobs", undefined, "jobs").then((d) => (d?.jobs || []).find((j) => j.id === id) || null, () => null);
    if (rec) { seen = true; missing = 0; if (rec.state !== "running" && rec.state !== "waiting") return rec; tick?.(rec); }
    else if (seen && ++missing >= 5) return null;
    await new Promise((r) => setTimeout(r, 700));
  }
}

// Who is asking (SPEC-model-slot-conflict.md §1): the Composer's own mic
// button is a shell surface with a fixed identity, the same way
// `playground/client.ts`'s `PLAYGROUND_REQUESTER` is — not the user settling
// a conflict with their own hands, and not another bot's page, so two bots
// dictating into their own composers at once are told apart from each other.
const COMPOSER_REQUESTER = "shell:bots-composer";

/**
 * POST /api/ai/transcribe, reading the error body itself: request() keeps only the message, and a `model_loading`
 * reply ({error: {type, message}, jobId}) names the download job to wait on. Transcribe's usual 409 is the plain
 * {error: "…"} (no model or no runner) and stays an error.
 */
async function transcribeStart(path: string): Promise<{ jobId: string; output: string }> {
  const r = await fetch("/api/ai/transcribe", { method: "POST", headers: { "X-Fused": "1", "X-Fused-Requester": COMPOSER_REQUESTER, "X-Fused-Requester-Label": COMPOSER_REQUESTER, "Content-Type": "application/json" }, body: JSON.stringify({ path }), cache: "no-store" });
  const data = await r.json().catch(() => ({})) as { jobId?: string; output?: string; job_id?: string; error?: string | { type?: string; message?: string; jobId?: string } };
  if (!r.ok) {
    const e = data.error;
    if (e && typeof e === "object") throw typed(e.message || r.statusText, e.type || "ai_error", e.jobId || data.jobId || data.job_id);
    throw typed(String(e || r.statusText), r.status === 409 ? "unavailable" : "bad_request");
  }
  if (!data.jobId || !data.output) throw typed("/api/ai/transcribe replied with no jobId", "ai_error");
  return { jobId: data.jobId, output: data.output };
}

async function transcribeOnce(path: string, note: Note): Promise<{ text: string }> {
  const started = await transcribeStart(path);
  const rec = await watchJob(started.jobId, undefined, (j) => note(jobNote(j)));
  if (rec && rec.state !== "done") {
    throw typed(rec.state === "cancelled" ? "the transcription was cancelled" : rec.message || "the transcription failed", rec.state === "cancelled" ? "cancelled" : "ai_error", started.jobId);
  }
  const written = await fetch(rawFileUrl(started.output), { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<{ text?: string }>;
  }).catch((cause) => { throw typed("the transcript could not be read: " + errMsg(cause), "ai_error", started.jobId); });
  return { text: written.text || "" };
}

async function micTranscribe(path: string, note: Note): Promise<{ text: string }> {
  try { return await transcribeOnce(path, note); }
  catch (e) {
    const t = e as TypedError;
    if (t?.type !== "model_loading" || !t.jobId) throw e;
    showBanner("Downloading the speech model (first time only). Your recording is kept; the text follows.");
    await watchJob(t.jobId, undefined, (j) => note(jobNote(j)));
    return transcribeOnce(path, note);
  }
}

export function Composer({ b, reply, setReply, threadRef }: ComposerProps) {
  const on = !!b;
  const ta = useRef<HTMLTextAreaElement>(null);
  const [pending, setPendingState] = useState<File[]>([]);
  const pendingRef = useRef<File[]>([]);
  const setPending = (f: File[]) => { pendingRef.current = f; setPendingState(f); };
  const [has, setHas] = useState(false);
  const [sending, setSending] = useState(false);
  // The command popup: the slash query the box holds (null: closed) and the highlighted row. Recomputed on every input
  // (fitInput), so dictation's dispatched input event and a paste follow the same rule as typing.
  const [cmd, setCmd] = useState<{ q: string; idx: number } | null>(null);
  // No popup while attachments are pending: the text is then a message naming the files ("/clear" sends as text), and
  // every path (Enter, Send, click) must agree, so the rows are simply not there.
  const cmds: Command[] = cmd && !pending.length ? matchCommands(cmd.q) : [];
  const cmdIdx = cmd ? Math.min(cmd.idx, Math.max(0, cmds.length - 1)) : 0;
  const lastVal = useRef("");  // the text the popup was last computed for (fitInput)
  const [drag, setDrag] = useState(false);
  const [micAvail, setMicAvail] = useState(false);
  // `note`: what the transcription is doing right now (jobNote), shown while busy so a first-run install is not a mute spinner.
  const [mic, setMic] = useState<{ rec: boolean; busy: boolean; time: string; note: string }>({ rec: false, busy: false, time: "", note: "" });
  const micRec = useRef<CaptureRec | null>(null), micTimer = useRef<ReturnType<typeof setInterval> | null>(null), micBusy = useRef(false);

  // Grow with the text (up to the CSS max), and reveal the send arrow when there is something to send.
  const fitInput = () => {
    const t = ta.current; if (!t) return;
    // Collapsing to 0 to measure briefly makes the thread above taller, and the browser clamps its scrollTop to fit;
    // putting the height back does not undo the clamp, so the messages jumped on every keystroke. Restore it.
    const th = threadRef.current, top = th ? th.scrollTop : 0;
    t.style.height = "0"; t.style.height = Math.max(36, t.scrollHeight) + "px";
    if (th && th.scrollTop !== top) th.scrollTop = top;
    setHas(t.value.trim().length > 0 || pendingRef.current.length > 0);
    // The popup follows the text, not every re-fit: a width change or an attachment re-fit on the same value leaves it
    // as it is, so Esc stays closed until the text changes.
    if (t.value !== lastVal.current) {
      lastVal.current = t.value;
      const q = slashQuery(t.value);
      setCmd((c) => q === null ? null : c && c.q === q ? c : { q, idx: 0 });
    }
  };
  useLayoutEffect(fitInput, [pending]);
  // The pane can get wider (gutter drag, preview toggle, window resize) after the text was measured; the old, taller
  // height would then stay and leave a blank band above the text. Re-fit whenever the width changes.
  useEffect(() => {
    const t = ta.current; if (!t) return;
    let lastW = 0;
    const ro = new ResizeObserver(([e]) => { const w = e.contentRect.width; if (w !== lastW) { lastW = w; fitInput(); } });
    ro.observe(t);
    return () => ro.disconnect();
  }, []);
  // A reply quote focuses the box.
  useEffect(() => { if (reply) ta.current?.focus(); }, [reply]);
  // Just handed the browser back (store noteFor, from the poll): a one-shot, optional "anything to add?" for 15 s. The box
  // takes focus once when it arrives; the placeholder only shows while the box is empty anyway.
  const noteFor = useBotsSelector((s) => s.noteFor);
  const [noteLive, setNoteLive] = useState(false);
  const noteFocused = useRef(0);  // the `until` already focused for: one focus per hand back, not per re-render / bot switch
  useEffect(() => {
    const left = noteFor && b && noteFor.id === b.id ? noteFor.until - Date.now() : 0;
    if (left <= 0) { setNoteLive(false); return; }
    setNoteLive(true);
    if (noteFocused.current !== noteFor!.until) { noteFocused.current = noteFor!.until; ta.current?.focus(); }
    const t = window.setTimeout(() => setNoteLive(false), left);
    return () => window.clearTimeout(t);
  }, [noteFor, b?.id]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ---- attachments ----
  const addFiles = (files: Iterable<File> | ArrayLike<File>) => {
    const next = [...pendingRef.current];
    for (const f of Array.from(files)) {
      if (f.size > ATTACH_MAX) { showBanner(`${f.name} is ${fmtBytes(f.size)}; attachments are limited to 8 MB.`); continue; }
      next.push(f);
    }
    setPending(next);
  };
  const fileInput = useRef<HTMLInputElement>(null);
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    addFiles(files.map((f) => (f.name && f.name !== "image.png" ? f : new File([f], pastedName(f), { type: f.type }))));
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer?.files || []); };

  // ---- dictation ----
  useEffect(() => {
    let dead = false;
    request<{ sources?: { audio?: { available?: boolean } } }>("GET", "/api/capture", undefined, "capture")
      .then((r) => { if (!dead && r?.sources?.audio?.available) setMicAvail(true); }, () => {});  // the button only exists when the machine can record
    return () => { dead = true; };
  }, []);
  const micUi = () => setMic((m) => ({ rec: !!micRec.current, busy: micBusy.current, time: micRec.current ? m.time : "", note: micBusy.current ? m.note : "" }));
  const micNote: Note = (text) => setMic((m) => (m.note === text ? m : { ...m, note: text }));
  const micStop = async (discard?: boolean) => {
    const rec = micRec.current; if (!rec) return;
    micRec.current = null; if (micTimer.current) clearInterval(micTimer.current); micTimer.current = null;
    if (discard) { micUi(); try { await request("POST", `/api/capture/${encodeURIComponent(rec.id)}/cancel`, {}, "capture"); } catch { /* gone */ } return; }
    micBusy.current = true; micUi(); micNote(jobNote(null));
    try {
      const took = await request<CaptureRec>("POST", `/api/capture/${encodeURIComponent(rec.id)}/stop`, {}, "capture")
        .catch((e) => { throw typed(errMsg(e), "capture_error"); });
      if (!took?.path) throw typed("the recording was not saved", "capture_error");
      if ((took.seconds ?? 1) < 0.5) return;  // a stray click, nothing worth transcribing
      const r = await micTranscribe(took.path, micNote);
      const text = (r?.text || "").replace(/\s+/g, " ").trim();
      if (text && ta.current) insertAtCaret(ta.current, text); else showBanner("Didn't catch any speech in that recording.");
    } catch (e) {
      const t = e as TypedError;
      if (t?.type === "cancelled") return;
      showBanner(t?.type === "capture_error" ? "The recording failed to save; please try again." : `Transcription failed: ${errMsg(e)}`);
    } finally {
      micBusy.current = false; micUi();
      if (ta.current && !ta.current.disabled) ta.current.focus();
    }
  };
  const micStopRef = useRef(micStop); micStopRef.current = micStop;
  const micStart = async () => {
    if (micBusy.current || micRec.current) return;
    let rec: CaptureRec;
    try { rec = await request<CaptureRec>("POST", "/api/capture/start", { mode: "audio", source: "mic", maxSeconds: MIC_MAX, title: "Dictation" }, "capture"); }
    catch (e) { showBanner((e as { status?: number })?.status === 409 ? errMsg(e) : `Could not start the microphone: ${errMsg(e)}`); return; }
    micRec.current = rec;
    const t0 = Date.now();
    setMic({ rec: true, busy: false, time: fmtSecs(0), note: "" });
    micTimer.current = setInterval(() => setMic((m) => ({ ...m, time: fmtSecs(Math.floor((Date.now() - t0) / 1000)) })), 500);
    // Hitting maxSeconds (or ✕ on the job row) ends the take without a click: follow the job so the button catches up.
    void watchJob(rec.jobId, () => micRec.current !== rec).then((j) => {
      if (micRec.current === rec && j?.state) void micStopRef.current(j.state === "cancelled");
    });
  };
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape" && micRec.current) void micStopRef.current(true); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); if (micTimer.current) clearInterval(micTimer.current); };
  }, []);

  // ---- slash commands ----
  // Run a command: the box empties (the text never reaches the bot as a message), the popup closes, the route runs.
  // The server is the real guard (Bot.clear_context refuses mid-task); act() banners its reason if the UI was stale.
  const runCommand = (c: Command) => {
    const t = ta.current; if (!t || !b) return;
    t.value = ""; fitInput(); setCmd(null);
    if (c.name === "clear") { setReply(null); setScrollToEnd(true); void act(() => api.clearContext(b.id)); }
    t.focus();
  };
  // Tab: complete the box to the highlighted command's name (the popup stays open on the exact name; Enter runs it).
  const completeCommand = (c: Command) => {
    const t = ta.current; if (!t) return;
    t.value = "/" + c.name; t.setSelectionRange(t.value.length, t.value.length); fitInput();
  };

  // ---- send ----
  const send = async () => {
    const t = ta.current; if (!t || !b) return;
    let text = t.value.trim();
    // A command runs instead of sending: the popup's highlighted row while it shows ("/cl" + Send), else the name typed
    // in full ("/clear"); the Send button and Enter converge here. With attachments pending the text is a message.
    const c = cmd && cmds.length ? cmds[cmdIdx] : commandFor(text);
    if (c && !pendingRef.current.length) { runCommand(c); return; }
    if (!text && !pendingRef.current.length) return;
    const files = pendingRef.current; setPending([]);
    const r = reply; setReply(null);
    t.value = ""; fitInput();
    setSending(true);
    try {
      const names: string[] = [];
      for (const f of files) {
        const res = await api.attach(b.id, f.name, await b64(f));
        if (res?.name) names.push(res.name);
      }
      if (names.length) text = (text ? text + "\n\n" : "") + `Attached (in your FILES folder, use \`upload\` when a page needs them): ${names.join(", ")}`;
      setScrollToEnd(true);   // consumed by the thread: show the message you just sent
      setBase(b.id, b.seq);   // sending resets the count: only what the bot says from here on is new
      markSeen(b.id, b.seq);  // and you have plainly read everything up to your own message
      await act(() => api.send(b.id, text, r ? r.seq : null));
      const th = threadRef.current; if (th) th.scrollTop = th.scrollHeight;
    } catch (e) {
      showBanner(errMsg(e));
      t.value = text; fitInput(); setReply(r);  // give the text (and the quote) back so nothing is lost
    } finally { setSending(false); }
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // An IME composition owns Enter/arrows/Esc (confirming a candidate must not send, nor run a command). Safari fires the
    // confirming keydown after compositionend with isComposing already false but keyCode 229 (same guard as live-input.ts).
    if (e.nativeEvent.isComposing || e.keyCode === 229 || e.key === "Process") return;
    // The popup takes the keys first while it shows rows: ↑↓ move, Tab completes, Enter runs, Esc closes (the text stays).
    if (cmd && cmds.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const d = e.key === "ArrowDown" ? 1 : -1;
        setCmd({ q: cmd.q, idx: (cmdIdx + d + cmds.length) % cmds.length });
        return;
      }
      if (e.key === "Tab") { e.preventDefault(); completeCommand(cmds[cmdIdx]); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); return; }  // send() runs the highlighted row: one path
      // Esc while the mic records belongs to the recording (the document listener above discards it); the popup stays.
      if (e.key === "Escape") { if (micRec.current) return; e.preventDefault(); setCmd(null); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); }
    if (e.key === "Escape" && reply) { e.preventDefault(); setReply(null); }
  };

  const running = b?.status === "running" || b?.status === "waiting";
  // The status line beside Pause / Stop: what the bot is doing and how long since its last step, so a long wait (a
  // login that takes minutes, a slow page) reads as "working, quiet for 3 min" rather than as a hang. Re-renders on
  // the poll (the selector) and once a second meanwhile for the clock.
  const lastStepTs = useBotsSelector(() => { const evs = b ? eventsOf(b.id) : []; for (let i = evs.length - 1; i >= 0; i--) if (evs[i].role !== "user" && evs[i].role !== "delivery") return evs[i].ts; return 0; });
  const [, tick] = useState(0);
  useEffect(() => { if (!running) return; const t = window.setInterval(() => tick((n) => n + 1), 1000); return () => window.clearInterval(t); }, [running]);
  const quiet = running && lastStepTs ? fmtAgo(lastStepTs) : "";
  const stat = !b || !running ? "" : b.status === "waiting" ? "Waiting for you · answer, sign in or approve above" : statusLabel(b) + (quiet && quiet !== "now" ? ` · last step ${quiet}` : "");
  // While you hold the browser the bot stays stopped: what you send is queued (server meta.held) and goes when you hand back.
  const placeholder = !b ? "Message…" : mic.busy ? mic.note || "Transcribing…"
    : b.control ? `Message ${b.name}… (sent when you hand back)`
    : reply ? "Reply…"
    : noteLive ? `Anything ${b.name} should know? Optional.`
    : b.status === "waiting" ? "The bot asked you a question — answer here"
    : running ? "Add an instruction mid-task…" : `Message ${b.name}`;
  const ctl = (fn: (id: string) => Promise<unknown>) => () => { if (b) void act(() => fn(b.id)); };

  return (
    <div className="composer">
      <div className="ctl">
        {stat ? <span className="stat" title={b?.title || undefined}><span className={`dot ${b?.status}`} />{stat}</span> : null}
        <button id="pause" disabled={!on || b?.status !== "running"} onClick={ctl(api.pause)}
          title="Hold the task before its next step. The browser and everything done so far stay; Resume carries on from here.">Pause</button>
        <button id="resume" disabled={!on || b?.status !== "paused"} onClick={ctl(api.resume)} title="Carry on from where it paused.">Resume</button>
        <button id="stop" className="danger" disabled={!on || !(running || b?.status === "paused")} onClick={ctl(api.stop)}
          title="End the task for good. What it found so far stays in the chat; a new message starts a fresh task.">Stop</button>
      </div>
      <div className={`chips${pending.length ? " show" : ""}`} id="attach">
        {pending.map((f, i) => (
          <span key={i} className="chip" title={f.name}><span>{f.name}</span><b>{fmtBytes(f.size)}</b>
            <button data-rm={i} title="Remove" onClick={() => setPending(pendingRef.current.filter((_, j) => j !== i))}>×</button></span>
        ))}
      </div>
      <div className={`row${has ? " has" : ""}${reply ? " replying" : ""}${drag ? " drag" : ""}`} id="crow"
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
        {/* The command popup (lib/commands): rows for the commands the slash word matches; the Clear context row carries
            canClear's reason when it cannot run now (still runnable: the server decides, act() banners its reason).
            mousedown is swallowed so the box keeps focus and the row runs on click. */}
        {cmd && cmds.length && b ? (
          <div className="cmds" id="ccmds" role="listbox">
            {cmds.map((c, i) => {
              const gate = c.name === "clear" ? canClear(b, eventsOf(b.id)) : { ok: true, why: "" };
              return (
                <button key={c.name} id={`ccmd-${c.name}`} role="option" aria-selected={i === cmdIdx} className={`${i === cmdIdx ? "sel" : ""}${gate.ok ? "" : " off"}`.trim() || undefined}
                  onMouseDown={(e) => e.preventDefault()} onMouseEnter={() => setCmd({ q: cmd.q, idx: i })} onClick={() => runCommand(c)}>
                  <code>/{c.name}</code><b>{c.title}</b><span>{gate.ok ? c.hint : gate.why}</span>
                </button>
              );
            })}
          </div>
        ) : null}
        <div className="quote" id="cquote" hidden={!reply}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 17H5a4 4 0 0 1 0-8h14" /><path d="m15 5 4 4-4 4" /></svg>
          <span id="cquotetext">{reply ? reply.text : ""}</span>
          <button id="cquotex" title="Cancel reply (Esc)" onClick={() => setReply(null)}>×</button>
        </div>
        <div className="line">
          <button id="attachbtn" disabled={!on} onClick={() => fileInput.current?.click()}
            title="Attach a file for the bot (or paste / drop one here). It lands in this bot's files folder, so it can upload it to a page.">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" /><path d="M8 13h5M8 17h6" /><path d="M20.5 9V4a2.5 2.5 0 0 0-5 0v8a3.5 3.5 0 0 0 7 0V5" /></svg>
          </button>
          <input type="file" id="attachfile" multiple hidden ref={fileInput}
            onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
          <button id="micbtn" hidden={!micAvail} disabled={!on && !mic.rec /* a running take must stay stoppable */}
            className={`${mic.rec ? "rec" : ""}${mic.busy ? " busy" : ""}`.trim() || undefined}
            title={mic.rec ? "Stop recording (Esc discards)" : mic.busy ? mic.note || "Transcribing…" : MIC_HINT}
            onClick={() => void (micRec.current ? micStop() : micStart())}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
            <span className="mictime" id="mictime">{mic.rec ? mic.time : ""}</span>
          </button>
          <textarea id="input" ref={ta} rows={1} placeholder={placeholder} disabled={!on}
            data-gramm="false" data-gramm_editor="false" data-enable-grammarly="false"
            // The command popup is a combobox listbox the box drives: the highlighted row is announced as the active descendant.
            role="combobox" aria-autocomplete="list" aria-expanded={!!(cmd && cmds.length)} aria-controls="ccmds"
            aria-activedescendant={cmd && cmds.length ? `ccmd-${cmds[cmdIdx].name}` : undefined}
            onInput={fitInput} onKeyDown={onKeyDown} onPaste={onPaste} />
          <button id="send" className="primary" title="Send (Enter)" disabled={!on || sending} onClick={() => void send()}>Send</button>
        </div>
      </div>
    </div>
  );
}
