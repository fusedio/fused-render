// The composer's three vocabularies and the precedence that resolves each one.
// Lists, labels and defaults are VERBATIM from T:11823-11912 (inventory
// 03 §F); the ranking is `curModel` / `curEffort` / `syncSelects`
// (T:11901-11912, 12171-12176):
//
//   this chat's RECORD  >  URL param  >  detected (`defaults` action)
//                        >  prefs default  >  constant
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
import { getPrefs, recordChatSettings } from "@platform/lib/api";
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
  /** The pane param AND this chat's record — never localStorage (03 §F).
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
  /** Both best-effort reads have landed (or failed). The "Fix with AI" boot
   *  branch awaits this before its automatic send (T:12619, 12635). */
  ready: boolean;
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
): ComposerDefaults {
  const snapshot = useChatParams(params);
  const [detected, setDetected] = useState<{ model: string; effort: string }>({
    model: "",
    effort: "",
  });
  const [pref, setPref] = useState("");
  // WHAT THE APP ITSELF WROTE DOWN for this conversation — the record that
  // outranks everything else here (see the header). It arrives with detection,
  // off the same `defaults` read, and is updated straight away on a pick so the
  // pill does not flicker back to its old value while the POST is in flight.
  const [recorded, setRecorded] = useState<{ model: string; effort: string }>({
    model: "",
    effort: "",
  });
  const [detectionReady, setDetectionReady] = useState(false);
  const [prefsReady, setPrefsReady] = useState(false);

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
  // "" — a chat with no session yet — is the one case that still asks the folder
  // question, because there is no conversation to ask instead. It is also the
  // case a host may seed (`ChatMount`'s `model`/`effort`, from the task's own
  // stored setting), and the case a pick cannot record: there is nothing to key
  // a record on until the first send mints an id, and that send records the pair
  // server-side (`agent._start`).
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

  useEffect(() => {
    if (!agentDir || !file) return;
    let live = true;
    pickedSinceRead.current = {};
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
        const rec = d?.recorded;
        const picked = pickedSinceRead.current;
        setRecorded({
          model: picked.model ?? (rec ? listedModel(rec.model) : ""),
          effort:
            picked.effort ??
            (rec && EFFORTS.includes(rec.effort as (typeof EFFORTS)[number])
              ? rec.effort
              : ""),
        });
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

  const model = resolveModel(
    snapshot.model,
    detected.model,
    pref,
    recorded.model,
  );
  const effort = resolveEffort(snapshot.effort, detected.effort, recorded.effort);
  const permission = resolvePermission(snapshot.permission);

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

  return useMemo(
    () => ({
      model,
      effort,
      permission,
      setModel: (value: string) => {
        params.set({ model: value });
        record({ model: value });
      },
      setEffort: (value: string) => {
        params.set({ effort: value });
        record({ effort: value });
      },
      setPermission: (value: PermissionMode) =>
        params.set({ permission: value }),
      ready: detectionReady && prefsReady,
    }),
    [model, effort, permission, params, record, detectionReady, prefsReady],
  );
}
