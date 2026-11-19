// SPECIALISTS — typed fetchers for every `/api/specialists…` route the UI calls
// (docs/specialists.md) and the shapes they answer with (B.2, B.3,
// B.6). Platform, not apps/specialists, because the task side of the feature
// lives inside the chat (apps/claude/specialists) and the shell's breakout card
// (shell/breakout/SpecialistBreakoutCard) calls them too, and neither may
// import another app.
//
// One transport: `getJson` / `mutateJson` from api.ts, so every POST carries the
// `X-Fused: 1` guard B.5 asks for and every failure is an `HttpError` whose
// message is the server's own `{"error": sentence}`.
import { getJson, mutateJson } from "./api";

// ── shapes ────────────────────────────────────────────────────────────────

export interface Triggers {
  folders: string[];
  keywords: string[];
}

/** specialist.json `face`: the shipped Canvas seed uses the bots' face object
 *  (`{shape, color, icon}`); a PATCH may store a plain string. Both are valid. */
export type SpecialistFace = string | { shape?: string; color?: string; icon?: string } | null;

/** A row of GET /api/specialists. */
export interface SpecialistSummary {
  id: string;
  name: string;
  subject: string;
  face: SpecialistFace;
  /** One line of status for the list ("Working on …", "Learned 3 things last night"). */
  status: string;
  working: boolean;
  /** The specialist's model, the default of its chats. Absent on an
   *  older server. */
  model?: string;
  /** Present when the server sends the triggers on the list (it may not). */
  triggers?: Triggers;
}

/** GET /api/specialists/{id}. */
export interface SpecialistDetail {
  id: string;
  name: string;
  subject: string;
  face: SpecialistFace;
  model: string;
  effort: string;
  triggers: Triggers;
  home: string;
  connections: string[];
  created: number;
  shipped: string;
  status: string;
}

/** Alias kept for B.9's name. */
export type Specialist = SpecialistDetail;

export interface FoldedSession {
  session_id: string;
  started: number;
  ended: number;
  title: string;
  messages: number;
}

/** `.specialist/chat.json` as GET/POST …/chat answer it. */
export interface SpecialistChat {
  current: string | null;
  folded: FoldedSession[];
}

export type ChangeKind =
  | "Learned"
  | "Corrected"
  | "Retired"
  | "Moved"
  | "New skill"
  | "New check"
  | "People"
  | "Trigger"
  | "Changed";

/** One item of a nightly change list (B.6, with the app's `id`, `commit`, `state`). */
export interface ChangeItem {
  id: string;
  kind: ChangeKind | string;
  text: string;
  entry?: string | null;
  file?: string;
  from?: string[];
  undo?: string;
  commit?: string;
  state?: "active" | "reverting" | "reverted" | string;
}

export interface LearnedRun {
  run: string;
  at: number;
  /** Already says "N entries were confirmed again" (those are counted, never items). */
  summary: string;
  items: ChangeItem[];
  /** Entries the update only confirmed again (their words did not change). */
  confirmed?: number;
}

export interface DraftTo {
  name: string;
  service: string;
  handle: string;
}

/** A draft to a person, waiting for the user (B.6). */
export interface Draft {
  id: string;
  at: number;
  to: DraftTo;
  subject: string;
  text: string;
  why: string;
  from?: { kind: string; run?: string };
  status: "waiting" | "sending" | "sent" | "discarded" | string;
}

export interface HelpingTask {
  session_id: string;
  title: string;
  file: string;
  breakout_id: string;
}

/** GET …/{id}/panel — the specialist page's right column. */
export interface SpecialistPanel {
  /** `new`: items of the updates newer than the last one opened in the Learned tab ("Learned · N new"). */
  learned: { run: string; at: number; summary: string; items: ChangeItem[]; total: number; new?: number } | null;
  /** Some update has run (finished, found nothing to do, or failed). Absent from an older server. */
  updated?: boolean;
  waiting: Draft[];
  inbox: { count: number };
  tasks: { helping_now: HelpingTask[]; guided_week: number };
}

export interface MemoryEntry {
  id: string;
  text: string;
  basis: string;
  seen: string | number;
  confirmed: string;
  from: string;
  retires: string;
  raw: string;
}

export interface SpecialistMemory {
  entries: MemoryEntry[];
  raw: string;
  lines: number;
  bytes: number;
  limit_lines: number;
  limit_bytes: number;
  knowledge: { title: string; path: string }[];
}

export type NoteKind = "gap" | "correction" | "convention" | "suspected-bug" | "behaviour" | "case";
export type NoteBasis = "user-said" | "verified" | "guess";

export interface NoteSource {
  type: string;
  session_id?: string;
  run_id?: string;
  line?: number;
  breakout_id?: string;
  receipt_id?: string;
  title?: string;
}

/** An inbox note (B.2). */
export interface Note {
  id: string;
  kind: NoteKind | string;
  claim: string;
  evidence?: string;
  basis: NoteBasis | string;
  entry?: string | null;
  outcome?: string | null;
  source: NoteSource;
  at: number;
  app_version?: string;
  digested?: number | null;
  /** True once the note reaches sessions and pulls (said.md). A note a TASK
   *  says the user said is false until the user confirms it (Use now). */
  in_effect?: boolean;
}

export interface SkillRow {
  name: string;
  description: string;
  path: string;
}

export interface PeopleRow {
  area: string;
  owner: string;
  service: string;
  handle: string;
  notes: string;
}

export interface ServiceRow {
  key: string;
  title: string;
  kind: "connect" | "app" | string;
  connected: boolean;
  allowed: boolean;
  // Optional per-tool facts, so a breakout's card can tell a read
  // (may offer "Approve, don't ask again") from a write (never). Absent: the
  // breakout offers nothing.
  tools?: { name: string; write: boolean }[];
}

export interface Counts {
  breakouts_opened: number;
  repeat_breakouts: number;
  pulls: number;
  pulls_confirmed: number;
  corrections_received: number;
  notes_by_source: Record<string, number>;
}

export interface Commit {
  sha: string;
  at: number;
  message: string;
  files: string[];
}

/** A task event (B.3): what a task's transcript shows a chip for. */
export interface TaskEvent {
  kind:
    | "pull"
    | "note"
    | "breakout_open"
    | "breakout_closed"
    | "breakout_discarded"
    | "breakout_deleted"
    | "stuck"
    | string;
  /** Epoch seconds. */
  at: number;
  specialist: { id: string; name: string };
  receipt_id?: string;
  /** `pull`: how many memory entries, skills and topic files were added. */
  entries?: number;
  skills?: number;
  knowledge?: number;
  trigger?: "choice" | "signal" | string;
  note_id?: string;
  undoable?: boolean;
  /** `note` events: the note's basis, and whether it is in effect yet. */
  basis?: string;
  in_effect?: boolean;
  breakout_id?: string;
  session_id?: string;
}

/** What a pull added, laid out for a person (GET …/receipts/{rid} `view`). */
export interface ReceiptView {
  memory?: { id?: string; text: string; basis?: string }[];
  said?: { text: string }[];
  knowledge?: { title: string }[];
  skills?: { name: string; description?: string }[];
}

export interface Brief {
  problem: string;
  tried: string;
  look_at: string;
}

/** A breakout record (B.3) plus the specialist's name. */
export interface BreakoutRecord {
  id: string;
  specialist: string;
  name?: string;
  origin: "offer" | "hand" | string;
  request_id?: string;
  parent: { session_id: string; run_id?: string; file: string; title?: string };
  brief: Brief;
  session_id: string;
  status: "open" | "closed" | "discarded" | "deleted" | string;
  opened: number;
  closes: BreakoutClose[];
  discarded: number | null;
}

/** Whether a close's summary draft still waits above the task's composer
 *  (docs/specialists.md §5). A close recorded before draft states has none. */
export type DraftState = "pending" | "sent" | "cleared";

/** One close of a breakout: its summary draft and where that draft stands. */
export interface BreakoutClose {
  at: number;
  note_id: string;
  draft: string;
  key?: string;
  draft_state?: DraftState;
}

export interface BreakoutCreated {
  breakout_id: string;
  home: string;
  seed: string;
  start_params: Record<string, string>;
  name: string;
}

export interface BreakoutClosed {
  draft: string;
  label: string;
  note_id: string;
  /** The close's key: what `setBreakoutDraft` names it by. */
  close?: string;
}

// ── routes ────────────────────────────────────────────────────────────────

export const SPECIALISTS_API = "/api/specialists";
const enc = encodeURIComponent;
const one = (id: string) => `${SPECIALISTS_API}/${enc(id)}`;
const post = <T>(url: string, body: unknown = {}) => mutateJson<T>("POST", url, body);
const put = <T>(url: string, body: unknown) => mutateJson<T>("PUT", url, body);
const del = <T>(url: string) => mutateJson<T>("DELETE", url);

// Core (stream 1)
export const listSpecialists = () => getJson<SpecialistSummary[]>(SPECIALISTS_API);
export const createSpecialist = (body: { name: string; subject: string; triggers?: Triggers }) =>
  post<SpecialistDetail>(SPECIALISTS_API, body);
export const getSpecialist = (id: string) => getJson<SpecialistDetail>(one(id));
export const patchSpecialist = (
  id: string,
  body: Partial<Pick<SpecialistDetail, "name" | "subject" | "face" | "model" | "effort" | "triggers">>,
) =>
  // The route is PATCH (docs/specialists.md §8); `mutateJson` speaks PUT/POST/DELETE only, so the
  // PATCH goes through fetch with the same guard header.
  patchJson<SpecialistDetail>(one(id), body);
export const deleteSpecialist = (id: string) => del<{ ok: true }>(one(id));
export const getChat = (id: string) => getJson<SpecialistChat>(`${one(id)}/chat`);
export const clearChat = (id: string) => post<SpecialistChat>(`${one(id)}/chat/clear`);
export const getCharter = (id: string) => getJson<{ text: string; path: string }>(`${one(id)}/charter`);
export const putCharter = (id: string, text: string) => put<{ text: string; path: string }>(`${one(id)}/charter`, { text });
export const listSkills = (id: string) => getJson<{ skills: SkillRow[] }>(`${one(id)}/skills`);
export const getSkill = (id: string, name: string) => getJson<{ name: string; text: string }>(`${one(id)}/skills/${enc(name)}`);
export const putSkill = (id: string, name: string, text: string) =>
  put<{ name: string; text: string }>(`${one(id)}/skills/${enc(name)}`, { text });
export const deleteSkill = (id: string, name: string) => del<{ ok: true }>(`${one(id)}/skills/${enc(name)}`);
export const getPeople = (id: string) => getJson<{ rows: PeopleRow[]; path: string }>(`${one(id)}/people`);
export const putPeople = (id: string, rows: PeopleRow[]) => put<{ rows: PeopleRow[]; path: string }>(`${one(id)}/people`, { rows });
export const getConnections = (id: string) => getJson<{ services: ServiceRow[] }>(`${one(id)}/connections`);
export const putConnections = (id: string, change: { add?: string[]; remove?: string[] }) =>
  put<{ services: ServiceRow[] }>(`${one(id)}/connections`, { add: change.add ?? [], remove: change.remove ?? [] });
export const getHistory = (id: string, path?: string) =>
  getJson<{ commits: Commit[] }>(`${one(id)}/history${path ? `?path=${enc(path)}` : ""}`);

// Task side (stream 2)
export const getTaskEvents = (sessionId: string) =>
  getJson<{ events: TaskEvent[] }>(`${SPECIALISTS_API}/task-events?session_id=${enc(sessionId)}`);
export const getReceipt = (id: string, rid: string) =>
  getJson<{ receipt: Record<string, unknown>; text: string; view?: ReceiptView }>(`${one(id)}/receipts/${enc(rid)}`);
export const createBreakout = (
  id: string,
  body: {
    parent: { session_id: string; run_id?: string; file: string; title?: string };
    brief: Brief;
    origin: "offer" | "hand";
    request_id?: string;
  },
) => post<BreakoutCreated>(`${one(id)}/breakouts`, body);
export const listBreakouts = (parentSessionId: string) =>
  getJson<{ breakouts: BreakoutRecord[] }>(`${SPECIALISTS_API}/breakouts?parent_session_id=${enc(parentSessionId)}`);
export const closePrompt = (id: string, bid: string) =>
  post<{ message: string }>(`${one(id)}/breakouts/${enc(bid)}/close-prompt`);
export const breakoutClosed = (id: string, bid: string, sessionId: string) =>
  post<BreakoutClosed>(`${one(id)}/breakouts/${enc(bid)}/closed`, { session_id: sessionId });
export const discardBreakout = (id: string, bid: string) =>
  post<{ ok: true }>(`${one(id)}/breakouts/${enc(bid)}/discard`);
/** The summary draft of one close was sent to the task or cleared: the record answers. */
export const setBreakoutDraft = (id: string, bid: string, close: string | number, state: "sent" | "cleared") =>
  post<BreakoutRecord>(`${one(id)}/breakouts/${enc(bid)}/draft`, { close, state });
export const reopenBreakout = (id: string, bid: string) =>
  post<{ session_id: string; start_params: Record<string, string>; home: string }>(
    `${one(id)}/breakouts/${enc(bid)}/reopen`,
  );

// Learning (stream 3)
export const getPanel = (id: string) => getJson<SpecialistPanel>(`${one(id)}/panel`);
export const getLearned = (id: string, limit = 5) => getJson<{ runs: LearnedRun[] }>(`${one(id)}/learned?limit=${limit}`);
export const revertChange = (id: string, run: string, item: string) =>
  post<{ message: string; session_id: string | null }>(`${one(id)}/changes/${enc(run)}/${enc(item)}/revert`);
export const runNightly = (id: string) => post<{ row: string }>(`${one(id)}/nightly/run`);
export const getNightly = (id: string) =>
  getJson<{
    last_ok: number | null;
    running: boolean;
    last_row: Record<string, unknown> | string | null;
    started?: number | null;
  }>(`${one(id)}/nightly`);
export const getMemory = (id: string) => getJson<SpecialistMemory>(`${one(id)}/memory`);
export const getInbox = (id: string) => getJson<{ notes: Note[] }>(`${one(id)}/inbox`);
/** `id` may be "general" — the general inbox for "no specialist fits". */
export const addNote = (
  id: string,
  body: {
    kind: NoteKind;
    claim: string;
    evidence?: string;
    basis: NoteBasis;
    source: { session_id: string; uuid?: string; title?: string };
  },
) => post<Note>(`${one(id)}/notes`, body);
export const deleteNote = (id: string, nid: string) => del<{ ok: true }>(`${one(id)}/notes/${enc(nid)}`);
/** A note a task said the user said, confirmed by the user: in effect now. */
export const confirmNote = (id: string, nid: string) => post<Note>(`${one(id)}/notes/${enc(nid)}/confirm`);
/** The general inbox: notes that fit no specialist. */
export const getGeneralInbox = () => getJson<{ notes: Note[] }>(`${SPECIALISTS_API}/general/inbox`);
export const moveGeneralNote = (nid: string, specialist: string) =>
  post<Note>(`${SPECIALISTS_API}/general/notes/${enc(nid)}/move`, { specialist });
export const listDrafts = (id: string) => getJson<{ drafts: Draft[] }>(`${one(id)}/drafts`);
export const sendDraft = (id: string, did: string) =>
  post<{ message: string; session_id: string | null }>(`${one(id)}/drafts/${enc(did)}/send`);
export const discardDraft = (id: string, did: string) => post<{ ok: true }>(`${one(id)}/drafts/${enc(did)}/discard`);
export const getCounts = (id: string) => getJson<Counts>(`${one(id)}/counts`);

/** PATCH with the same guard and error contract as `mutateJson`. */
async function patchJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    body: JSON.stringify(body),
  });
  const data = (await res.json()) as T & { error?: unknown };
  if (!res.ok) {
    const said = data && typeof data.error === "string" ? data.error : "";
    const err = new Error(said || `HTTP ${res.status}`) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return data;
}

/** The specialist behind this route was deleted (410 `reason: "deleted"`,
 *  or a 404 / 410 for the specialist itself). */
export function isSpecialistDeleted(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const e = err as { status?: number; body?: { reason?: unknown } | null };
  if (e.body && typeof e.body === "object" && e.body.reason === "deleted") return true;
  return e.status === 410;
}

const FACE_PALETTE = ["#1f8f7e", "#3b6fd8", "#9a5bd6", "#c4683b", "#2f8f3f", "#b8456b", "#6b7a1f", "#2f7f9f"];

/** A specialist face's colour: a `#rgb`/`#rrggbb` in `face` (the string, or the
 *  object's `color`), else a stable pick from the id. One rule for every place
 *  a face is drawn (the page, the breakout card). */
export function specialistFaceColor(face: SpecialistFace | undefined, id: string): string {
  const raw = typeof face === "string" ? face : face && typeof face === "object" ? face.color : "";
  const c = typeof raw === "string" ? raw.trim() : "";
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(c)) return c;
  let h = 0;
  for (const ch of id || "x") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FACE_PALETTE[h % FACE_PALETTE.length]!;
}

/** A thrown fetch error as one sentence for the UI. */
export function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return "Something went wrong. Try again.";
}

// ── wording shared by the page and the task side ──────────────────────────

/** The revert request (`REVERT_PREFIX`), used only when the server's answer has
 *  no message of its own. The change's text is the first quoted string, which
 *  is what the thread shows ("Asked to undo: <text>"). */
export function revertMessage(item: ChangeItem, _run?: string): string {
  return (
    `${REVERT_PREFIX}"${String(item.text || "").replace(/"/g, "'")}" (${item.kind}). ` +
    `How to undo it: ${item.undo || "put it back as it was"}. Change only that, never restore whole files from git, ` +
    "keep the notes it came from, and tell me in plain words what you changed."
  );
}

// ── The fixed first characters of app-written messages (docs/specialists.md §5) ──
// The backend pins these with tests; the thread renders by them.
export const SEED_PREFIX = 'Brief from the task "';
export const CLOSE_OUT_PREFIX = "The user is closing this breakout";
export const REVERT_PREFIX = "Undo this change from the latest update: ";
export const SEND_DRAFT_PREFIX = "Review and send this draft to ";

/** The services key in an `mcp__specialist_services__<key>_<tool>` tool name. */
export const SERVICE_TOOL_PREFIX = "mcp__specialist_services__";

/** Sanitised as B.4 sanitises tool names, so a key can be matched against one. */
export function sanitizeToolPart(s: string): string {
  return s.replace(/[^A-Za-z0-9_-]/g, "_");
}

/** Which allowed-list key a services tool belongs to: the longest service key
 *  whose sanitised form prefixes the tool's own part. */
// The server names service tools `<key>_<tool>` (sanitised, possibly
// truncated with a hash). There is no route that maps a tool name back to its
// key, so the card matches it against GET …/connections. A truncated name of a
// very long key would not match and the button is then not drawn.
export function serviceKeyForTool(tool: string, services: readonly ServiceRow[]): string | null {
  if (!tool.startsWith(SERVICE_TOOL_PREFIX)) return null;
  const rest = tool.slice(SERVICE_TOOL_PREFIX.length);
  let best: string | null = null;
  for (const s of services) {
    const k = sanitizeToolPart(s.key);
    if (rest.startsWith(k + "_") && (!best || k.length > sanitizeToolPart(best).length)) best = s.key;
  }
  return best;
}
