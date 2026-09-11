// The composer's three vocabularies and the precedence that resolves each one.
// Lists, labels and defaults are VERBATIM from T:11823-11912 (inventory
// 03 §F); the ranking is `curModel` / `curEffort` / `syncSelects`
// (T:11901-11912, 12171-12176):
//
//   URL param  >  detected (`defaults` action)  >  prefs default  >  constant
//
// and every answer is validated against the list the pill offers, in the
// ACCESSOR rather than at the sync site: an unknown `?model=` would otherwise
// set a value matching no option, which renders as a blank pill (fitSelect
// returns early with no `selectedOptions[0]`) and is also what reaches the CLI.
import { useEffect, useMemo, useState } from "react";
import { getPrefs } from "@platform/lib/api";
import { runAgent } from "../protocol/agent";
import type { DefaultsResponse, PermissionMode } from "../protocol/types";
import type { ParamsStore } from "../params/store";
import { useChatParams } from "../params/useChatParams";

/** `claude --model`'s vocabulary, holding BOTH shapes the flag accepts: a
 *  PINNED full id (leads — someone opening this menu is usually after a
 *  specific model) and a floating alias under it (T:11823). */
export const MODELS = [
  "claude-fable-5-1",
  "fable",
  "opus",
  "sonnet",
  "haiku",
] as const;
export const MODEL_LABELS: Record<string, string> = {
  "claude-fable-5-1": "Fable 5.1",
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

/** `curModel()` (T:11901). */
export function resolveModel(
  param?: string,
  detected?: string,
  pref?: string,
): string {
  return pick(
    MODELS,
    param || detected || pref || DEFAULT_MODEL,
    DEFAULT_MODEL,
  );
}
/** `curEffort()` (T:11905) — prefs never reach effort, only detection does. */
export function resolveEffort(param?: string, detected?: string): string {
  return pick(EFFORTS, param || detected || DEFAULT_EFFORT, DEFAULT_EFFORT);
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
  /** Persistence is the pane PARAM and nothing else — no localStorage
   *  (03 §F). Fired even when the value is unchanged: picking the value
   *  detection guessed is how the user PINS it (T:12542-12547). */
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
  const [detectionReady, setDetectionReady] = useState(false);
  const [prefsReady, setPrefsReady] = useState(false);

  useEffect(() => {
    if (!agentDir || !file) return;
    let live = true;
    void runAgent(agentDir, "defaults", { file }, { key: null })
      .then((out) => {
        if (!live) return;
        const d = out as DefaultsResponse;
        setDetected({
          model:
            d && MODELS.includes(d.model as (typeof MODELS)[number])
              ? d.model
              : "",
          effort:
            d && EFFORTS.includes(d.effort as (typeof EFFORTS)[number])
              ? d.effort
              : "",
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
  }, [agentDir, file]);

  useEffect(() => {
    let live = true;
    void getPrefs()
      .then((p) => {
        const m = p.model?.default;
        if (live && m && MODELS.includes(m as (typeof MODELS)[number]))
          setPref(m);
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

  const model = resolveModel(snapshot.model, detected.model, pref);
  const effort = resolveEffort(snapshot.effort, detected.effort);
  const permission = resolvePermission(snapshot.permission);

  return useMemo(
    () => ({
      model,
      effort,
      permission,
      setModel: (value: string) => params.set({ model: value }),
      setEffort: (value: string) => params.set({ effort: value }),
      setPermission: (value: PermissionMode) =>
        params.set({ permission: value }),
      ready: detectionReady && prefsReady,
    }),
    [model, effort, permission, params, detectionReady, prefsReady],
  );
}
