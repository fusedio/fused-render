// The composer's three vocabularies and the precedence that resolves each one.
// Lists, labels and defaults are VERBATIM from T:11823-11912 (inventory
// 03 §F); the ranking is `curModel` / `curEffort` / `syncSelects`
// (T:11901-11912, 12171-12176):
//
//   this chat's RECORD  >  seed param  >  the GLOBAL pair / detected
//                        >  prefs default  >  constant
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
import { getPrefs, readChatSettings, recordChatSettings } from "@platform/lib/api";
import {
  getClaudeDefaults,
  readClaudeDefaults,
  setClaudeDefaults,
  subscribeClaudeDefaults,
  type ClaudeDefaults,
} from "@platform/lib/claude-defaults";
import { listedModelIn } from "@platform/lib/model-vocab";
import { runAgent } from "../protocol/agent";
import type { DefaultsResponse, PermissionMode } from "../protocol/types";
import type { ParamsStore } from "../params/store";
import { useChatParams } from "../params/useChatParams";

/** `claude --model`'s vocabulary, one entry per model (T:11823).
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
/** The strictest mode is the default: more auto-approval is opted into, never
 *  handed out by a missing param (T:11911). */
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
function pickModel(want: string): string {
  return listedModelIn(want, MODELS) || DEFAULT_MODEL;
}

/** The same fold for an answer that is VALIDATED rather than resolved — the
 *  `defaults` read, this chat's record, the prefs default. "" for anything this
 *  build does not offer, which is precisely "no opinion" and leaves the rank
 *  below it speaking. */
function listedModel(value: string | null | undefined): string {
  return listedModelIn(value, MODELS);
}

/** `curModel()` (T:11901), with the chat's own record ahead of it.
 *
 *  `recorded` is LAST in the list and FIRST in the ranking, deliberately: the
 *  three below it are T's own order and are pinned by tests that quote it, so
 *  the new rank is appended rather than threaded through them. See the header
 *  for why a record outranks a param. */
export function resolveModel(
  param?: string,
  detected?: string,
  pref?: string,
  recorded?: string,
): string {
  return pickModel(recorded || param || detected || pref || DEFAULT_MODEL);
}
/** `curEffort()` (T:11905) — prefs never reach effort, only detection does.
 *  `recorded` leads, exactly as it does for the model above. */
export function resolveEffort(
  param?: string,
  detected?: string,
  recorded?: string,
): string {
  return pick(
    EFFORTS,
    recorded || param || detected || DEFAULT_EFFORT,
    DEFAULT_EFFORT,
  );
}
/** `syncSelects`'s permission branch (T:12171). */
export function resolvePermission(param?: string): PermissionMode {
  return PERMISSION_MODES.includes(param as PermissionMode)
    ? (param as PermissionMode)
    : DEFAULT_PERMISSION;
}

export interface ComposerDefaults {
  model: string;
  effort: string;
  permission: PermissionMode;
  /** The pane param AND this chat's record — never localStorage (03 §F) — or,
   *  for a chat with no session and no host seed, the GLOBAL pair itself
   *  (2026-09-21): there is no conversation to record against, so the pick is a
   *  statement about what this machine opens next, and it goes to the one file
   *  that holds that. See `pickGlobal` in the body.
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
  /** Every best-effort read has landed (or failed) — the record, detection and
   *  the prefs. The "Fix with AI" boot branch awaits this before its automatic
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
 * Detection (`defaults`) and the prefs read are both best-effort and both end
 * in a re-resolve, so whichever lands second simply re-renders the same
 * ranking (T:12608-12645).
 */
export function useComposerDefaults(
  agentDir: string | null,
  file: string | null,
  params: ParamsStore,
  hostSeeded = false,
): ComposerDefaults {
  const snapshot = useChatParams(params);
  const [detected, setDetected] = useState<{ model: string; effort: string }>({
    model: "",
    effort: "",
  });
  const [pref, setPref] = useState("");
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
  const [detectionReady, setDetectionReady] = useState(false);
  const [prefsReady, setPrefsReady] = useState(false);
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
  const pickedSinceRead = useRef<{ model?: string; effort?: string }>({});

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
    setRecordReady(false);
    if (!sessionId) {
      setRecordReady(true);
      return;
    }
    void readChatSettings(sessionId)
      .then((rec) => {
        if (!live) return;
        const picked = pickedSinceRead.current;
        setRecorded({
          model: picked.model ?? listedModel(rec?.model),
          effort:
            picked.effort ??
            (rec && EFFORTS.includes(rec.effort as (typeof EFFORTS)[number])
              ? rec.effort
              : ""),
        });
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
    if (!agentDir || !file) return;
    let live = true;
    pickedSinceRead.current = {};
    // Same rule as the fast read: a detection is about ONE conversation. Left
    // standing across a session change, `detectionReady` would vouch for the
    // previous chat's pair and a chat with no record would paint it, then flip
    // when its own answer landed (Bugbot, PR #1226).
    setDetected({ model: "", effort: "" });
    setDetectionReady(false);
    void runAgent(agentDir, "defaults",
                  sessionId ? { file, session_id: sessionId } : { file },
                  { key: null })
      .then((out) => {
        if (!live) return;
        const d = out as DefaultsResponse;
        setDetected({
          model: d ? listedModel(d.model) : "",
          effort:
            d && EFFORTS.includes(d.effort as (typeof EFFORTS)[number])
              ? d.effort
              : "",
        });
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
        const recModel = rec ? listedModel(rec.model) : "";
        const recEffort =
          rec && EFFORTS.includes(rec.effort as (typeof EFFORTS)[number])
            ? rec.effort
            : "";
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
  }, [agentDir, file, sessionId]);

  useEffect(() => {
    let live = true;
    void getPrefs()
      .then((p) => {
        const m = listedModel(p.model?.default);
        if (live && m) setPref(m);
      })
      .catch(() => {
        // Same footing as detection.
      })
      .finally(() => {
        if (live) setPrefsReady(true);
      });
    return () => {
      live = false;
    };
  }, []);

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
  const model = resolveModel(
    paramModel,
    globModel || detected.model,
    pref,
    recorded.model,
  );
  const effort = resolveEffort(
    paramEffort,
    globEffort || detected.effort,
    recorded.effort,
  );
  const permission = resolvePermission(snapshot.permission);

  // ── WHEN A PILL MAY SHOW ITS VALUE ────────────────────────────────────────
  //
  // Per FIELD, and the question is never "has everything answered" but "can
  // anything still in flight CHANGE this one". Read straight off the ranking
  // above:
  //
  //   * a record for this field, or a `?model=`/`?effort=` the host seeded —
  //     both outrank every read that is still out, so the field is settled the
  //     moment the (fast) record read has answered;
  //   * neither — then the next rank down is detection, and for the model the
  //     prefs default behind it, so the slow reads have to land first.
  //
  // An unlisted param is settled too, and deliberately: `resolveModel` folds it
  // to the constant rather than falling through, so nothing pending speaks for
  // that field either.
  //
  // THE GLOBAL READ IS A THIRD WAY TO SETTLE, and for a brand-new chat it is
  // the usual one: it is fast, and it outranks both slow reads. A global that
  // answers "" for a field settles nothing — the ranks below it are detection
  // and the prefs, and those are still out.
  const recordAnswered = recordReady;
  const modelSettled =
    recordAnswered &&
    (!!recorded.model || !!paramModel || !!globModel ||
      (globalReady && detectionReady && prefsReady));
  const effortSettled =
    recordAnswered &&
    (!!recorded.effort || !!paramEffort || !!globEffort ||
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
    (settings: { model?: string; effort?: string }) => {
      if (!sessionId) return;
      pickedSinceRead.current = { ...pickedSinceRead.current, ...settings };
      setRecorded((prev) => ({ ...prev, ...settings }));
      void recordChatSettings(sessionId, settings).catch(() => {});
    },
    [sessionId],
  );

  // A PICK ON A CHAT THAT DOES NOT EXIST YET IS A PICK OF THE GLOBAL VALUE
  // (Akshil, 2026-09-21). There is no conversation to key a record on, and the
  // address bar is not storage — it died with the tab, and while it lived it
  // shadowed the very setting the New task card was showing. So the write goes
  // to `~/.claude/settings.json`, the one home this pair has, and every other
  // open surface hears it (`claude-defaults` announces it).
  //
  // NOT for a host-seeded mount: a peek on a task that has not run is showing
  // that TASK's stored setting, and moving its pill is not a statement about
  // every future chat on this machine. It keeps writing the param it always
  // did, which is the memory store that mount owns.
  //
  // The stale param is CLEARED on the way, and only in the case that no longer
  // reads it: a `?model=`/`?effort=` left in the URL by an older build would
  // otherwise sit in every copied link saying something the app has stopped
  // believing. `replace`, because removing our own leftovers is not a
  // navigation the Back button should have to undo.
  const globalPick = !sessionId && !hostSeeded;
  const pickGlobal = useCallback(
    (patch: { model?: string; effort?: string }) => {
      setGlob((prev) => ({ ...prev, ...patch }));
      params.set(
        { model: null, effort: null },
        { history: "replace" },
      );
      void setClaudeDefaults(patch);
    },
    [params],
  );

  return useMemo(
    () => ({
      model,
      effort,
      permission,
      setModel: (value: string) => {
        if (globalPick) {
          pickGlobal({ model: value });
          return;
        }
        params.set({ model: value });
        record({ model: value });
      },
      setEffort: (value: string) => {
        if (globalPick) {
          pickGlobal({ effort: value });
          return;
        }
        params.set({ effort: value });
        record({ effort: value });
      },
      setPermission: (value: PermissionMode) =>
        params.set({ permission: value }),
      ready: detectionReady && prefsReady && recordReady && globalReady,
      pillsReady,
      modelSettled,
      effortSettled,
    }),
    [model, effort, permission, params, record, globalPick, pickGlobal,
     detectionReady, prefsReady, recordReady, globalReady, pillsReady,
     modelSettled, effortSettled],
  );
}
