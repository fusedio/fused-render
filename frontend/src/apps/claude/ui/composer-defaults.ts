// The composer's three vocabularies and the precedence that resolves each one.
// Lists, labels and defaults are VERBATIM from T:11823-11912 (inventory
// 03 §F); the ranking is `curModel` / `curEffort` / `syncSelects`
// (T:11901-11912, 12171-12176):
//
//   this chat's RECORD  >  seed param  >  the GLOBAL pair / detected
//                        >  constant
//
// T ranked a "prefs default" between detection and the constant. That rank is
// gone with fused-render's own `default_model` preference: the GLOBAL pair
// above IS the user's default model (Claude Config › Model edits it), and a
// second, lower-ranked default was a setting that only spoke when the first
// said nothing.
//
// "seed param" and not "URL param" since 2026-09-21: a `?model=`/`?effort=`
// counts only when a HOST stated it (ChatMount's props, into a per-mount memory
// store) or when this chat already has a session. A bare one on the Explorer's
// shell URL is the composer's own leftover from an earlier visit and no longer
// speaks — see `seedCounts` in the body. The GLOBAL pair
// (~/.claude/settings.json `model`/`effortLevel`, via
// platform/lib/claude-defaults) sits at the detection rank for a chat with no
// session, which is the same answer `agent._defaults` gives that case, only
// fast and live.
//
// The record leads and it is the one rank that is not from T. It is what the
// app itself wrote down for THIS conversation — every spawn, every send and
// every pill pick (`tasks_store`'s `session_settings.json`, reached through
// `agent._defaults` and `recordChatSettings`) — and it outranks the params
// because the params are a SEED: the New task card and "Fix with AI" build
// deep links carrying `?model=`/`?effort=`, which answer for a chat that does
// not exist yet and must stand down the moment it does. Left the other way
// round, reopening a task undid a pill its reader had moved mid-chat.
//
// and every answer is validated against the list the pill offers, in the
// ACCESSOR rather than at the sync site: an unknown `?model=` would otherwise
// set a value matching no option, which renders as a blank pill (fitSelect
// returns early with no `selectedOptions[0]`) and is also what reaches the CLI.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { readChatSettings, recordChatSettings } from "@platform/lib/api";
import {
  getClaudeDefaults,
  readClaudeDefaults,
  subscribeClaudeDefaults,
  type ClaudeDefaults,
} from "@platform/lib/claude-defaults";
import { listedModelIn, normalizeModel } from "@platform/lib/model-vocab";
import { narrowPermission, useDefaultPermission } from "../feature-flag";
import {
  capsOf,
  defaultEffortFor,
  defaultModelOf,
  effortsForModel,
  isBackendEnabled,
  modelsOf,
  useAgentBackends,
} from "@platform/lib/agent-backends";
import { runAgent } from "../protocol/agent";
import type { DefaultsResponse, PermissionMode } from "../protocol/types";
import type { ParamsStore } from "../params/store";
import { useChatParams } from "../params/useChatParams";

/** `claude --model`'s vocabulary, one entry per model (T:11823).
 *
 *  THE STATIC CLAUDE ROW since the agent catalog (docs/COPILOT_BACKEND.md
 *  D11): the live lists are `modelsOf(backend)` / `effortsForModel(backend, model)` off
 *  `platform/lib/agent-backends`, and these constants are what every reader
 *  sees before the catalog lands and what the Claude row says once it has.
 *
 *  A pinned full id ("claude-fable-5-1") used to lead this list, above the
 *  floating alias for the same model. It named the same thing twice, so the
 *  menu asked a question with one answer (Akshil, 2026-09-18). Every spelling
 *  of Fable resolves onto the alias now — see `normalizeModel`, which is what
 *  keeps a chat, a task or a `?model=` that still carries the old id reading as
 *  Fable instead of blanking the pill. */
export const MODELS = ["fable", "opus", "sonnet", "haiku"] as const;
export const MODEL_LABELS: Record<string, string> = {
  fable: "Fable",
  opus: "Opus",
  sonnet: "Sonnet",
  haiku: "Haiku",
};

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

/** agent.py's PERMISSION_MODES keys; `plan` leads because it is the strictest
 *  end and reads best there (T:11844). */
export const PERMISSION_MODES: readonly PermissionMode[] = [
  "plan",
  "prompt",
  "acceptEdits",
  "auto",
];
export const PERMISSION_LABELS: Record<PermissionMode, string> = {
  plan: "plan first",
  prompt: "ask every time",
  acceptEdits: "auto-accept edits",
  auto: "Claude decides",
};
/** The same labels with the AGENT named in the one that names it ("Copilot
 *  decides", D18). The Claude row is `PERMISSION_LABELS` byte for byte. */
export function permissionLabelsFor(agent: string): Record<PermissionMode, string> {
  return agent === "Claude" ? PERMISSION_LABELS : { ...PERMISSION_LABELS, auto: `${agent} decides` };
}
/** The DISTINGUISHING word of each label, never a truncation — for the pill
 *  only, and only when the row has run out of width (T:11858-11870). The menu
 *  always spells the sentences out. */
export const PERMISSION_SHORT: Record<PermissionMode, string> = {
  plan: "plan",
  prompt: "ask",
  acceptEdits: "edits",
  auto: "decides",
};

export const DEFAULT_MODEL = "sonnet";
export const DEFAULT_EFFORT = "medium";
/** The SAFETY fallback only: a mangled or unknown value reads as the strictest
 *  mode, so a bad param never buys more auto-approval than was picked
 *  (T:11911). An ABSENT value is not this — it is the user's default
 *  (`claude_default_permission` pref, "auto" unless changed), see
 *  `resolvePermission`. */
export const DEFAULT_PERMISSION: PermissionMode = "prompt";

/** Non-selectable headings naming what each list IS (T:11935-11938). */
export const GROUP_LABELS = {
  model: "Model",
  effort: "Effort",
  permission: "Approvals",
} as const;
/** Pill accessible names (03 §F). */
export const PILL_ARIA = {
  model: "Model",
  effort: "Effort",
  permission: "How much Claude may do without asking",
} as const;

function pick(list: readonly string[], want: string, fallback: string): string {
  return list.includes(want) ? want : fallback;
}

/** `pick` for the model list, with every Fable spelling folded onto the one
 *  option that offers it first. Normalising here rather than at each call site
 *  is what makes a legacy `claude-fable-5-1` — in a record, a param or a
 *  transcript — a Fable pill everywhere at once. */
function pickModel(want: string, list: readonly string[] = MODELS, fallback = DEFAULT_MODEL): string {
  return listedModelIn(want, list) || fallback;
}

/** `curModel()` (T:11901), with the chat's own record ahead of it and T's
 *  prefs rank removed (see the header).
 *
 *  `recorded` is LAST in the list and FIRST in the ranking, deliberately: the
 *  two below it are T's own order and are pinned by tests that quote it, so
 *  the new rank is appended rather than threaded through them. See the header
 *  for why a record outranks a param. */
export function resolveModel(
  param?: string,
  detected?: string,
  recorded?: string,
  /** The AGENT's list and its default (D11) — the Claude constants when
   *  absent, which is every caller that predates the catalog. */
  list: readonly string[] = MODELS,
  fallback = DEFAULT_MODEL,
): string {
  return pickModel(recorded || param || detected || fallback, list, fallback);
}
/** `curEffort()` (T:11905) — prefs never reach effort, only detection does.
 *  `recorded` leads, exactly as it does for the model above. */
export function resolveEffort(
  param?: string,
  detected?: string,
  recorded?: string,
  list: readonly string[] = EFFORTS,
  preferred?: string,
): string {
  // A model that takes no effort has NO value here — "" is "no flag", and the
  // pill is not drawn — whatever a record or a param still carries from a
  // model that did.
  if (!list.length) return "";
  const fallback = preferred && list.includes(preferred)
    ? preferred
    : list.includes(DEFAULT_EFFORT) ? DEFAULT_EFFORT : list[0];
  return pick(list, recorded || param || detected || fallback, fallback);
}
/** `syncSelects`'s permission branch (T:12171). The first ABSENT value ends
 *  the ranking — `recorded` (this chat's own pick) over `param` over the
 *  user's `globalDefault` pref over "auto" — and the winner is narrowed: one
 *  that is present but not a mode is the safety "prompt", never a quiet fall
 *  through to a looser rank. */
export function resolvePermission(
  param?: string,
  recorded?: string,
  globalDefault?: string,
): PermissionMode {
  const wanted = recorded || param || globalDefault || "auto";
  return narrowPermission(wanted) || DEFAULT_PERMISSION;
}

export interface ComposerDefaults {
  model: string;
  effort: string;
  permission: PermissionMode;
  /**
   * WHICH AGENT THE NEXT TURN RUNS ON (docs/COPILOT_BACKEND.md D2/D18): the
   * session's own, once the wire has named it (`ChatState.backend`, handed in
   * as `sessionBackend`), else the `backend` param — the Agent pill's pick for
   * a chat that has no session yet — else the catalog's default. The model
   * and effort lists are THIS agent's (`modelsOf`/`effortsForModel`), so a value the
   * other agent recorded folds to this one's default rather than a blank pill.
   */
  backend: string;
  /** The session has a turn: the agent is a fact, and the pill fixes on it. */
  backendFixed: boolean;
  /** The pane param AND this chat's record — never localStorage (03 §F) — or,
   *  for a chat with no session and no host seed, this mount's own `local`
   *  state (2026-10-01): there is no conversation to record against yet, and
   *  the pick is about THIS chat only — never the global pair, which the Claude
   *  config page alone edits. See `pickLocal` in the body.
   *
   *  The param alone was the whole of it, and it is not persistence: it dies
   *  with the address bar, so a pick was lost on the next open and detection
   *  answered instead. The record is the durable half now
   *  (`recordChatSettings`), written only once the chat has a session to key it
   *  on. Fired even when the value is unchanged: picking the value detection
   *  guessed is how the user PINS it (T:12542-12547) — and with a record behind
   *  it, pinning finally means something. */
  setModel(value: string): void;
  setEffort(value: string): void;
  setPermission(value: PermissionMode): void;
  /** The Agent pill's pick — a PARAM only (the first send records it per
   *  session, `agent._start`), and refused once `backendFixed`. */
  setBackend(value: string): void;
  /** Every best-effort read has landed (or failed) — the record, detection and
   *  the global pair. The "Fix with AI" boot branch awaits this before its automatic
   *  send (T:12619, 12635), so the record read is IN it: an automatic send is
   *  the one send a human cannot hold back, and it must not launch on a value
   *  the record was about to overturn. */
  ready: boolean;
  /** THE PILLS MAY PAINT THEIR VALUE.
   *
   *  Weaker than `ready` and deliberately so: a pill is settled as soon as
   *  nothing still in flight can CHANGE it, which for the overwhelming majority
   *  of chats is the record read alone (milliseconds). Only a field the record
   *  left "" — and that no URL param seeded either — has to wait for the slow
   *  `defaults` read, because detection is the next rank down.
   *
   *  Until this is true the composer draws the pills in a loading state rather
   *  than a value, which is the whole of the fix: a pill that has shown nothing
   *  cannot flip. */
  pillsReady: boolean;
  /** The two halves of `pillsReady`, for the one caller that must not tie them
   *  together: what a send carries. A model settled by a task's `?model=` must
   *  reach the CLI even while the effort is still being looked up. */
  modelSettled: boolean;
  effortSettled: boolean;
}

/**
 * The three pills' resolved values, kept in step with the params store.
 * Detection (`defaults`) and the global read are both best-effort and both end
 * in a re-resolve, so whichever lands second simply re-renders the same
 * ranking (T:12608-12645).
 */
export function useComposerDefaults(
  file: string | null,
  params: ParamsStore,
  hostSeeded = false,
  /** `ChatState.backend` — the conversation's own agent, when the wire has
   *  said; "" otherwise. Outranks the param and never moves (D2). */
  sessionBackend = "",
): ComposerDefaults {
  const snapshot = useChatParams(params);
  // ── WHICH AGENT, and therefore WHICH LISTS ────────────────────────────────
  // Subscribed so a Preferences switch or the catalog's first read re-resolves
  // the pills; a `backend` param naming a switched-off agent is ignored rather
  // than offered (D17).
  const catalog = useAgentBackends();
  // THE CONVERSATION'S OWN AGENT, from the first source that has said: the
  // wire (`sessionBackend`), this chat's record or `defaults` answer
  // (`knownBackend`, below), or — for a chat WITH a session — the host's
  // seed (`ChatMount backend`, the task row's). All three are facts about
  // the session, so they count even for an agent switched off (D17: the
  // chat still reads, named as what it is). Only a NEW chat's pick is
  // checked against the switches.
  const [knownBackend, setKnownBackend] = useState("");
  const hasSession = !!snapshot.session_id;
  const ownBackend = sessionBackend || knownBackend || (hasSession ? snapshot.backend || "" : "");
  const paramBackend = snapshot.backend && isBackendEnabled(snapshot.backend) ? snapshot.backend : "";
  const backend = ownBackend || paramBackend || catalog.default;
  const models = modelsOf(backend);
  const fallbackModel = defaultModelOf(backend);
  const [detected, setDetected] = useState<{ model: string; effort: string }>({
    model: "",
    effort: "",
  });
  // WHAT THE APP ITSELF WROTE DOWN for this conversation — the record that
  // outranks everything else here (see the header). It arrives off its OWN
  // read (`GET /api/tasks/settings`, one JSON file, milliseconds) rather than
  // off the `defaults` subprocess, which is what lets the pills wait for it
  // instead of painting a constant and swapping it two seconds later. It is
  // updated straight away on a pick so the pill does not flicker back to its
  // old value while the POST is in flight.
  const [recorded, setRecorded] = useState<{ model: string; effort: string }>({
    model: "",
    effort: "",
  });
  // The Approvals pick this chat recorded ("" none). Its own state beside
  // `recorded`, because only this field also has to reach the params the
  // controller sends from (see the fast read below).
  const [recordedPermission, setRecordedPermission] = useState("");
  const globalPermission = useDefaultPermission();
  const [detectionReady, setDetectionReady] = useState(false);
  // THE FAST HALF, and the one the pills actually wait on. See `pillsReady`.
  const [recordReady, setRecordReady] = useState(false);

  // WHICH CONVERSATION THE PILLS ARE ABOUT, and it is the subject of every
  // question this hook asks. Detection used to name only the FOLDER, and
  // `agent._defaults` answered with the model last used anywhere in it — so the
  // same chat reached from the Tasks peek, from its Open button, from a row or
  // from a bare URL could each be told a different thing, and a model the reader
  // had picked in THIS chat lost to one some other chat in the same folder used
  // more recently (Akshil, 2026-09-18: "what I select as a user stays"). Named,
  // the agent answers from this chat's own record and then its own transcript,
  // and from nothing else — a field neither knows comes back "" and the
  // constants below speak, rather than a neighbour chat's value.
  //
  // "" — a chat with no session yet — is answered from the GLOBAL Claude
  // preference (~/.claude/settings.json's `model`/`effortLevel`), because there
  // is no conversation to ask instead and nothing about the folder is a
  // statement about what this window should run (Akshil, 2026-09-21). It is
  // also the case a host may seed (`ChatMount`'s `model`/`effort`, from the
  // task's own stored setting), and the case a pick cannot record: there is
  // nothing to key a record on until the first send mints an id, and that send
  // records the pair server-side (`agent._start`).
  const sessionId = snapshot.session_id || "";

  // PICKS MADE WHILE A `defaults` READ IS IN FLIGHT. The read is asked at mount
  // and again when the chat learns its id; a pill moved in that window is
  // recorded server-side by `record` below, but the answer already on the wire
  // was composed BEFORE that write and lands after it. Let it overwrite
  // `recorded` and the pill snaps back to the value the reader just left — and
  // because `recorded` outranks the param, the next send would run (and
  // re-record) that stale value. So each field picked since the read began is
  // remembered here and wins over the read's copy of it; cleared when a new
  // read starts, because a new read is about a new conversation.
  const pickedSinceRead = useRef<{ model?: string; effort?: string; permission?: string }>({});

  // ── THE GLOBAL PAIR — the answer for a chat that has no conversation yet ──
  //
  // `~/.claude/settings.json`'s `model` / `effortLevel`, read (and written)
  // through `platform/lib/claude-defaults`, which is the single home the New
  // task card shares. It is the SAME answer the slow `defaults` read gives a
  // sessionless chat — `agent._global_defaults` is literally what that action
  // falls through to now — but it arrives off one JSON endpoint in
  // milliseconds instead of a subprocess in seconds, and it MOVES: another
  // surface changing it has to reach a composer that is already open.
  //
  // Consulted only while this chat has no session. A conversation that exists
  // answers for itself (record, then its own transcript) and the global must
  // not speak over it.
  const [glob, setGlob] = useState<ClaudeDefaults>(
    () => getClaudeDefaults() ?? { model: "", effort: "" },
  );
  // Whether this chat's own read has ANSWERED, for the same reason
  // `recordReady` exists: a pill must not paint a constant it is about to
  // replace. A document that has already asked once (another composer, the New
  // task card) starts settled, so the second surface paints on its first render.
  const [globalReady, setGlobalReady] = useState(() => getClaudeDefaults() !== null);
  // This mount's own pick for a chat that has no session yet (see `pickLocal`).
  const [local, setLocal] = useState<ClaudeDefaults>({ model: "", effort: "" });
  const prevSessionId = useRef(sessionId);
  useEffect(() => {
    if (sessionId) {
      // A conversation that exists answers for itself. Nothing here is waited
      // on, and nothing here speaks.
      setGlobalReady(true);
      return;
    }
    // Back to a chat with NO session ("Fix with AI" clears `session_id` on
    // boot): the `true` the branch above set is not an answer for this chat.
    // Settled only if some surface in this document has already read.
    setGlobalReady(getClaudeDefaults() !== null);
    let live = true;
    const off = subscribeClaudeDefaults(setGlob);
    // Always re-asked on open, never served purely from the module cache: this
    // is the "opening either surface shows the current value" half of the
    // contract, and the settings page can have written the file since.
    void readClaudeDefaults()
      .then((d) => {
        if (live) setGlob(d);
      })
      .finally(() => {
        if (live) setGlobalReady(true);
      });
    return () => {
      live = false;
      off();
    };
  }, [sessionId]);

  // ── THE FAST READ: this chat's record, straight off the store ─────────────
  //
  // ONE JSON FILE READ over HTTP (`GET /api/tasks/settings`), asked on mount and
  // again the moment the chat learns its id. It answers the rank that outranks
  // everything else here, and it answers it in milliseconds — which is the whole
  // of the flip fix. The slow `defaults` read below spawns agent.py as a
  // subprocess to scan a transcript tail; it took two to three seconds, and the
  // pills had already painted a constant by then, so every open of a chat showed
  // one value and then swapped it (Akshil, 2026-09-19).
  //
  // A chat with NO SESSION is answered here rather than asked: there is no
  // conversation to have a record, so the record is "" for both fields and it is
  // known synchronously. It still has to be MARKED ready, because `pillsReady`
  // waits on this flag for every chat.
  //
  // The stale-read guard applies to THIS read as much as to the slow one, and
  // for the same reason: the pill can be moved while a read is in flight, the
  // pick is recorded server-side, and an answer composed before that write lands
  // after it. `pickedSinceRead` is cleared HERE — a new conversation is what
  // invalidates it, and this is the effect that knows the conversation changed
  // even when the agent dir has not arrived yet.
  useEffect(() => {
    let live = true;
    pickedSinceRead.current = {};
    // A NEW CONVERSATION STARTS FROM NOTHING. The previous chat's pair is
    // cleared before this read goes out, so neither a slow answer nor a failed
    // one can leave it standing under the new session (Bugbot, PR #1226): the
    // pills go back to the wash and paint this chat's own record, or "".
    setRecorded({ model: "", effort: "" });
    setRecordedPermission("");
    setKnownBackend("");
    setRecordReady(false);
    // THE LOCAL PICK FOLLOWS EXACTLY ONE TRANSITION: "no session" → "the id
    // this chat was just given". A fresh chat (back to "") opens on the global
    // pair again, and a switch straight from one conversation to another must
    // not carry a pick the first one made into a chat whose record is empty
    // (review, 2026-10-01).
    if (!(sessionId && !prevSessionId.current)) setLocal({ model: "", effort: "" });
    prevSessionId.current = sessionId;
    if (!sessionId) {
      setRecordReady(true);
      return;
    }
    void readChatSettings(sessionId)
      .then((rec) => {
        if (!live) return;
        const picked = pickedSinceRead.current;
        // Stored NORMALISED, validated at resolve time against the AGENT's
        // list (below): the list depends on which agent this chat runs on,
        // and that can be learned after this read lands.
        setRecorded({
          model: picked.model ?? normalizeModel(rec?.model),
          effort: picked.effort ?? (rec?.effort || ""),
        });
        if (rec?.backend) setKnownBackend((b) => b || rec.backend!);
        // THE PICK SURVIVES A REMOUNT: the controller sends from the param, so
        // the recorded mode is written back into it (only when it differs, and
        // replace-style, so this is neither a history entry nor a loop).
        const recPermission = picked.permission ?? narrowPermission(rec?.permission);
        setRecordedPermission(recPermission || "");
        if (recPermission && params.get("permission") !== recPermission) {
          params.set({ permission: recPermission }, { history: "replace" });
        }
      })
      .catch(() => {
        // Best-effort, like every other read here: a record we could not fetch
        // is a record that says nothing, and the ranks below it speak.
      })
      .finally(() => {
        if (live) setRecordReady(true);
      });
    return () => {
      live = false;
    };
  }, [sessionId]);

  useEffect(() => {
    if (!file) return;
    let live = true;
    pickedSinceRead.current = {};
    // Same rule as the fast read: a detection is about ONE conversation. Left
    // standing across a session change, `detectionReady` would vouch for the
    // previous chat's pair and a chat with no record would paint it, then flip
    // when its own answer landed (Bugbot, PR #1226).
    setDetected({ model: "", effort: "" });
    setDetectionReady(false);
    void runAgent("defaults", sessionId ? { file, session_id: sessionId } : { file })
      .then((out) => {
        if (!live) return;
        const d = out as DefaultsResponse;
        setDetected({
          model: d ? normalizeModel(d.model) : "",
          effort: d?.effort || "",
        });
        // The conversation's agent off its record (§8) — only when there IS
        // a conversation: the folder-wide answer names no one's.
        if (sessionId && d?.backend) setKnownBackend((b) => b || d.backend!);
        // Validated against the same two lists, and for the same reason: a
        // record naming something this build does not offer renders as a blank
        // pill. An agent that predates the field simply has none, which reads
        // as "no record" — exactly what it means.
        // THE SAME RECORD, SECOND. The fast read above has almost always
        // answered by now and this is the same file read twice — but the agent
        // is the only reader when the fast door is missing (an older server) or
        // failed, so it still teaches. PER FIELD, and never backwards: a field
        // this answer has no value for leaves the one already learned alone,
        // which is what stops a `defaults` answer composed before a pill pick
        // from erasing it a second time.
        const rec = d?.recorded;
        const picked = pickedSinceRead.current;
        const recModel = rec ? normalizeModel(rec.model) : "";
        const recEffort = rec?.effort || "";
        setRecorded((prev) => ({
          model: picked.model ?? (prev.model || recModel),
          effort: picked.effort ?? (prev.effort || recEffort),
        }));
      })
      .catch(() => {
        // Best-effort: the pills keep the fallback rather than showing nothing.
      })
      .finally(() => {
        if (live) setDetectionReady(true);
      });
    return () => {
      live = false;
    };
    // `sessionId` IS A DEP: a chat that starts without one learns it seconds
    // later (the CLI reports it, `run-controller` writes it into the params),
    // and the first answer was about the folder. Re-asking then is what makes a
    // conversation's own settings appear as soon as it has an identity —
    // and it is one cheap read of one transcript's tail.
  }, [file, sessionId]);


  // ── WHEN A `?model=`/`?effort=` STILL COUNTS ──────────────────────────────
  //
  // The params are a SEED, and the question this answers is WHOSE seed.
  //
  //   * A HOST STATED IT — `ChatMount`'s `model`/`effort` props, written once
  //     into a per-mount MEMORY store: the Tasks side peek and the cards wall
  //     handing over the task's own stored setting. That is a real statement
  //     about a real conversation (a task booked and not yet run), it is the
  //     whole reason those props exist (Akshil, 2026-09-18, "the values there
  //     were different"), and it keeps outranking everything but this chat's
  //     own record.
  //
  //   * NOBODY DID — a bare `?model=opus&effort=high` on the Explorer's shell
  //     URL. That is not a seed at all: it is the composer's OWN pick from some
  //     earlier visit, which used to be written straight into the address bar
  //     and then read back as if a host had asked for it. It is exactly the bug
  //     in Akshil's screenshot (2026-09-21) — the composer showing Opus / high
  //     off a stale URL while the New task card showed the real global pair —
  //     so for a chat with no session and no host seed the params no longer
  //     speak, and a pick no longer writes them (see `setModel` below).
  //
  // A chat that HAS a session is unchanged either way: its record leads, and
  // the params rank under it exactly as they always have.
  const seedCounts = !!sessionId || hostSeeded;
  const paramModel = seedCounts ? snapshot.model : "";
  const paramEffort = seedCounts ? snapshot.effort : "";
  // The global pair rides in AT THE DETECTION RANK, and it is the same answer:
  // `agent._defaults` for a chat with no session id IS `_global_defaults`. This
  // is the fast copy of it, so the pills settle in milliseconds instead of
  // seconds, and it is the copy that hears another surface's write.
  const globModel = sessionId ? "" : glob.model;
  const globEffort = sessionId ? "" : glob.effort;
  // THE PICK THIS NEW CHAT MADE rides just under its record: it is the record
  // the first send is about to write, so it has to outrank the global and the
  // reads, and it has to keep answering across the seconds between the CLI
  // reporting a session id and that session's record read landing.
  // VALIDATED AGAINST THIS AGENT'S LIST, rank by rank, so a value the list
  // does not hold leaves the rank below it speaking (what `listedModel` did
  // per read before the list depended on the agent). Only the PARAM folds to
  // the default instead — see `resolveModel`'s header.
  const lm = (v: string) => listedModelIn(v, models);
  const model = resolveModel(
    paramModel,
    lm(globModel) || lm(detected.model),
    lm(recorded.model) || lm(local.model),
    models,
    fallbackModel,
  );
  // THE EFFORTS ARE THE MODEL'S, so they are read once the model is known:
  // per model where the catalog knows them per model (Copilot's `auto` and
  // Claude ids take none; its GPT ids each name their levels), the agent's
  // list otherwise. An empty list resolves to "" and hides the pill.
  const efforts = effortsForModel(backend, model);
  const le = (v: string) => (efforts.includes(v) ? v : "");
  const effort = resolveEffort(
    paramEffort,
    le(globEffort) || le(detected.effort),
    le(recorded.effort) || le(local.effort),
    efforts,
    defaultEffortFor(backend, model),
  );
  // An agent with no plan mode (§7 `plan_mode`) never shows "plan": the pill
  // has no such row for it, and the send would be refused.
  const resolvedPermission = resolvePermission(snapshot.permission, recordedPermission, globalPermission);
  const permission =
    resolvedPermission === "plan" && !capsOf(backend).plan_mode ? DEFAULT_PERMISSION : resolvedPermission;

  // ── WHEN A PILL MAY SHOW ITS VALUE ────────────────────────────────────────
  //
  // Per FIELD, and the question is never "has everything answered" but "can
  // anything still in flight CHANGE this one". Read straight off the ranking
  // above:
  //
  //   * a record for this field, or a `?model=`/`?effort=` the host seeded —
  //     both outrank every read that is still out, so the field is settled the
  //     moment the (fast) record read has answered;
  //   * neither — then the next rank down is detection, so the slow read has
  //     to land first.
  //
  // An unlisted param is settled too, and deliberately: `resolveModel` folds it
  // to the constant rather than falling through, so nothing pending speaks for
  // that field either.
  //
  // THE GLOBAL READ IS A THIRD WAY TO SETTLE, and for a brand-new chat it is
  // the usual one: it is fast, and it outranks both slow reads. A global that
  // answers "" for a field settles nothing — the rank below it is detection,
  // and that is still out.
  //
  // ASKED OF THE LISTED VALUES (`lm`/`le`), the same ones the resolve above
  // ranks by: a record naming a model THIS agent does not list speaks for
  // nothing there, so it must not settle the pill either — it would paint the
  // fallback, then flip when a slow read landed, and an early send would carry
  // the fallback. The PARAM stays raw, per the note above.
  const recordAnswered = recordReady;
  const modelSettled =
    recordAnswered &&
    (!!lm(recorded.model) || !!lm(local.model) || !!paramModel || !!lm(globModel) ||
      (globalReady && detectionReady));
  // A model that takes no effort has nothing in flight that could change the
  // (absent) pill, so its field is settled with the model's.
  const effortSettled =
    recordAnswered &&
    (efforts.length === 0 ||
      !!le(recorded.effort) || !!le(local.effort) || !!paramEffort || !!le(globEffort) ||
      (globalReady && detectionReady));
  const pillsReady = modelSettled && effortSettled;

  // A PICK IS A WRITE, not just a param. The param still moves — it is what the
  // rest of the page reads this render, and what a copied URL carries — but it
  // is the record that survives leaving the page and that every other door into
  // this chat reads first. Written per field, so moving the effort cannot erase
  // the model the spawn recorded.
  //
  // Optimistically, then over the wire: the pill has to show the new value on
  // this render, and the record is what it now ranks by. A failed POST is left
  // alone rather than rolled back — the param says the same thing, so the pill
  // is right either way until the next `defaults` read settles it — and it must
  // not be an error the user sees: nothing about this send has failed.
  const record = useCallback(
    (settings: { model?: string; effort?: string; permission?: string }) => {
      if (!sessionId) return;
      pickedSinceRead.current = { ...pickedSinceRead.current, ...settings };
      const { permission: pickedPermission, ...pair } = settings;
      if (pickedPermission) setRecordedPermission(pickedPermission);
      if (pair.model !== undefined || pair.effort !== undefined) {
        setRecorded((prev) => ({ ...prev, ...pair }));
      }
      void recordChatSettings(sessionId, settings).catch(() => {});
    },
    [sessionId],
  );

  // A PICK ON A CHAT THAT DOES NOT EXIST YET IS THIS CHAT'S PICK, NOT THE
  // GLOBAL'S (Akshil, 2026-10-01, reversing 2026-09-21: "any change in that
  // model and thinking should only affect that instance of the new chat"). The
  // global pair in `~/.claude/settings.json` is edited from the Claude config
  // page and nowhere else. So the pick lives in `local` — component state,
  // ranked right under this chat's record — until the first send records it
  // per session (`_start` writes `session_settings.json`), and it is dropped
  // the moment this mount goes back to "no session" (a fresh chat opens on the
  // global pair again). Not the address bar: a `?model=` on the shell URL
  // outlived the tab and shadowed the real global on the next visit, which was
  // the bug of 2026-09-21, so a stale one is still CLEARED on the way.
  //
  // NOT for a host-seeded mount: a peek on a task that has not run is showing
  // that TASK's stored setting, and it keeps writing the param it always did,
  // which is the memory store that mount owns.
  const localPick = !sessionId && !hostSeeded;
  const pickLocal = useCallback(
    (patch: { model?: string; effort?: string }) => {
      setLocal((prev) => ({ ...prev, ...patch }));
      params.set(
        { model: null, effort: null },
        { history: "replace" },
      );
    },
    [params],
  );

  // THE AGENT PICK IS A PARAM, NOT A RECORD: the first send records it per
  // session (`agent._start`), and until then the pill's own word is the only
  // place it lives. The local model/effort picks go with it — they were picks
  // from the other agent's menu.
  const setBackend = useCallback(
    (value: string) => {
      if (ownBackend || !isBackendEnabled(value)) return;
      setLocal({ model: "", effort: "" });
      // …and a "plan" pick goes too where the new agent has no plan mode: the
      // send reads the param itself, and would ask for a mode it refuses.
      const dropPlan = params.get("permission") === "plan" && !capsOf(value).plan_mode;
      params.set({ backend: value, ...(dropPlan ? { permission: null } : {}) }, { history: "replace" });
    },
    [params, ownBackend],
  );

  return useMemo(
    () => ({
      model,
      effort,
      permission,
      backend,
      backendFixed: !!ownBackend,
      setBackend,
      setModel: (value: string) => {
        if (localPick) {
          pickLocal({ model: value });
          return;
        }
        params.set({ model: value });
        record({ model: value });
      },
      setEffort: (value: string) => {
        if (localPick) {
          pickLocal({ effort: value });
          return;
        }
        params.set({ effort: value });
        record({ effort: value });
      },
      setPermission: (value: PermissionMode) => {
        params.set({ permission: value });
        record({ permission: value });
      },
      ready: detectionReady && recordReady && globalReady,
      pillsReady,
      modelSettled,
      effortSettled,
    }),
    [model, effort, permission, backend, ownBackend, setBackend, params, record,
     localPick, pickLocal, detectionReady, recordReady, globalReady,
     pillsReady, modelSettled, effortSettled],
  );
}
