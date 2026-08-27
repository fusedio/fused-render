// The status bar's Models section (D565): the PERSISTENT half of the three —
// what is resident in memory right now, and what it costs — left of Activity
// and Updates, which are transient work that appears and resolves (see
// StatusBar.tsx's own header comment for the lifetime-ordering principle this
// follows, the same one NotificationHost.tsx documents for its own column).
//
// A QUICK-INFO POPOVER, NOT A MANAGEMENT CONSOLE (user call, revising an
// earlier gauge/progress-bar draft: "we don't need a gauge if too
// complicated. just a quick info upon clicking which we have a list of
// loaded models we can unload"). The chip is plain text — the count and the
// cost, nothing else; the panel is one row per resident model, its name, its
// own resident bytes, and an Unload button. No gauge, no proportional fill,
// no RAM fraction — that is the whole feature.
//
// NO NEW TRANSPORT: `useAiRuntime` (apps/ai_models/lib/aiRuntime.ts) already
// polls `GET /api/ai/runtime` — the same shared poll GlobalSidebar's own
// resident-model dot reads, so this section costs no second request, and its
// own docstring already sanctions polling it ("so the sidebar can poll it").
// `unloadAiModel` (platform/lib/api.ts) already wraps `POST
// /api/ai/runtime/unload` with the D3 `X-Fused` guard every other mutation in
// the app carries.
//
// WHY THIS FILE LIVES IN shell/, NOT platform/ — the same reason
// QueueDock.tsx/RepoUpdatesDock.tsx do: `aiRuntime.ts` is apps/ai_models
// territory, and platform may not import apps (frontend/scripts/
// check-boundaries.mjs); shell may import anything, so the shell composes
// this section and hands it to `StatusBar` as `models`, same as the other
// two.
//
// SPLIT INTO A PURE VIEW (`ModelsCardView`) AND A STATEFUL WRAPPER
// (`ModelsDock`, default export) — the same split DownloadManagerView/
// RepoUpdatesCardView use, for the identical reason: no polling, no network,
// no `window`/`document`, so ModelsDock.test.tsx can render the view
// directly with a fixed model list rather than mocking `useAiRuntime`.
import { useRef, useState } from "react";
import { unloadAiModel, type AiLoadedModel } from "@platform/lib/api";
import { formatSize, repoName } from "@platform/lib/format";
import { aiRuntimeSettled, publishAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";

// This section's own persisted collapse preference — a THIRD independent key
// beside `fused-render:jobs-collapsed` and `fused-render:repo-updates-collapsed`
// (DownloadManager.tsx / RepoUpdatesDock.tsx), for the identical reason those
// two are separate: three sections with three separate histories a user
// might want folded independently.
const NOOP = () => {};

const COLLAPSED_KEY = "fused-render:models-collapsed";

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false; // private mode / disabled storage — expanded is the honest default
  }
}

function saveCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* best-effort, like every other persisted chrome flag */
  }
}

// Reuses `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-amount`/`.dl-status` —
// the job row's own classes (DownloadManager.tsx) — rather than a parallel
// `m-` set: a model row is shaped exactly like a job row (a name, a number,
// an action, an optional status line under it), and notifications.css
// already draws that shape correctly in both themes. `.dl-row-cancel` is
// Unload's, the same "text, not a ✕" control JobRow's own Cancel wears
// (round 1: two controls, one meaning each) — Unload is a distinct verb
// from Cancel, but the same visual language: a row-scoped, quietly-styled
// text button, not a glyph.
function ModelRow({
  model,
  onUnload,
}: {
  model: AiLoadedModel;
  onUnload: (model: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  // Unload is a REAL, immediate, work-destroying action — it frees weights
  // that may take minutes to reload — but the panel is small and the action
  // is recoverable by loading again, so there is no confirmation step, only
  // an honest in-flight label (mirrors JobRow's own Cancel -> "Cancelling…")
  // and a failure that says so rather than doing nothing visible.
  const [failure, setFailure] = useState<string | null>(null);

  const unload = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await onUnload(model.model);
    } catch {
      setFailure("Could not unload — check your connection and retry.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dl-row">
      <div className="dl-row-head">
        {/* The MODEL name only, not the whole `owner/model` repo id — the
            same trim `.dl-model` uses on the job row, for the same reason:
            the owner never distinguishes anything and eats width a narrow
            bar has none of. Full id stays on hover. */}
        <span className="dl-title" title={model.model}>
          {repoName(model.model)}
        </span>
        {/* A non-ready worker holds no weights yet, so `residentBytes` is
            null and `formatSize` yields "" — an empty column that reads as a
            glitch. Its STATE is the honest thing to show there instead
            (venv/starting/downloading/loading), and the bring-up's real
            progress (percentage, cancel) is a job row in Jobs, reported by
            `supervisor._report` (D588). */}
        <span className="dl-amount">
          {model.state === "ready" ? formatSize(model.residentBytes) : model.state}
        </span>
        <button className="dl-row-cancel" onClick={unload} disabled={busy}>
          {busy ? "Unloading…" : "Unload"}
        </button>
      </div>
      {failure && <div className="dl-status">{failure}</div>}
    </div>
  );
}

/**
 * The pure, props-in half — see DownloadManagerView's own doc for why this
 * split exists. Every entry the caller hands in is drawn, whatever its own
 * `state` (venv/starting/downloading/loading/ready/error) — this is a quick
 * "what's resident" readout, not a state machine viewer.
 */
export function ModelsCardView({
  models,
  collapsed,
  onToggle,
  onClose,
  onUnload,
}: {
  models: AiLoadedModel[];
  collapsed: boolean;
  onToggle: () => void;
  /** Background the panel — an outside pointer-down or Escape (D574).
   *  Optional: a caller that mounts this view directly need not dismiss. */
  onClose?: () => void;
  onUnload: (model: string) => Promise<void>;
}) {
  // READY MODELS ONLY decide the chip (D588). `loaded` includes workers in
  // venv/starting/downloading/loading, and those carry `residentBytes: null`
  // (supervisor.py sums the same field, so its `totalResidentBytes` is null
  // too) — which made a mid-bring-up chip fall back to the bare label,
  // pixel-identical to idle but NOT muted. That was the third state the user
  // was reading as confusing. Bring-up already reports a job row via
  // `supervisor._report`, so it belongs in Jobs where it has a percentage and
  // a cancel; a half-loaded model with no number here is strictly worse.
  //
  // Derived locally rather than from the server's `totalResidentBytes` (that
  // prop is gone) so the number and the "ready only" rule cannot disagree.
  const residentBytes = models.reduce(
    (sum, m) => sum + (m.state === "ready" ? (m.residentBytes ?? 0) : 0),
    0,
  );
  // EXACTLY TWO CHIP STATES, keyed on the one value the chip shows: muted
  // `Models` when there is nothing resident, `Models · 5.6 GB` when there is.
  // Keyed on the BYTES, not on `models.length` or a ready count, so a ready
  // worker whose runner reported no size cannot produce a third, unmuted
  // bare-label state either.
  const idle = residentBytes === 0;
  // The PANEL still lists every worker whatever its state — that is where a
  // bring-up is legitimately visible, with its state as the detail (ModelRow
  // above). So the panel's emptiness is its own question, not the chip's.
  const panelEmpty = models.length === 0;
  // Wraps the chip AND the panel — dismissOnOutside.ts explains why the whole
  // host, not just the panel, is what counts as "inside".
  const hostRef = useRef<HTMLDivElement | null>(null);
  useDismissOnOutside(hostRef, !collapsed, onClose ?? NOOP);

  return (
    <div className="dl-host" ref={hostRef}>
      {/* ALWAYS a real, clickable button now (D573, user: "the chevron
          doesn't belong to the status bar. lets follow vscode/cursor for
          inspiration" — the bar shows the category NAME plus a count, and
          the idle sentence moves into the panel; see `DownloadManagerView`'s
          own header comment for the fuller reasoning, identical here). No
          label prefix once there is a value beyond the bare count (code
          review revision, still true): the name IS "Models" either way now,
          so the only thing that changes between idle and active is the
          trailing count and cost. */}
      <button
        className={"dl-toggle" + (idle ? " is-idle" : "")}
        onClick={onToggle}
        aria-expanded={!collapsed}
        title={collapsed ? "Show loaded models" : "Hide loaded models"}
      >
        {/* NO INDICATOR OF ANY KIND (D588, user: "lets just remove the circle
            from models item", after "I see many different states for the
            model item ... this is confusing"). Not the outlined circle Jobs
            and Notifications carry — Models has no count, so it has no
            emptiness to indicate that its own muted label does not already
            say — and not the filled `.dl-new-dot` either, which is deleted
            app-wide: D587 had already established this chip as a state
            READOUT that never auto-opens, because a resident model is a
            consequence of an action the user just took, and an indicator
            announcing that same event contradicted it. This settles the
            question rather than reopening it: label, optional size, nothing
            else. The only treatments left are `.is-idle`'s muting and the
            hover / `aria-expanded` wash. */}
        <span className="dl-summary">
          {`Models${residentBytes ? ` · ${formatSize(residentBytes)}` : ""}`}
        </span>
      </button>
      {!collapsed && (
        <div className="dl-panel">
          {panelEmpty ? (
            <div className="dl-panel-empty">No models loaded</div>
          ) : (
            <div className="dl-rows">
              {models.map((m) => (
                <ModelRow key={m.model} model={m} onUnload={onUnload} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ModelsDock() {
  const runtime = useAiRuntime();
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  // Same wiring DownloadManagerView/RepoUpdatesCardView use — a dot for a
  // model that loaded while this chip was collapsed, AND (D574) a transient
  // auto-open of this section's own panel so the arrival is on screen rather
  // than only hinted at. `autoOpen` is never persisted — see autoExpand.ts.
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    runtime.loaded.map((m) => m.model),
    collapsed,
    // Not `runtime.loaded.length > 0` — an idle machine must still announce
    // its first real load (autoExpand.ts's `ready`).
    aiRuntimeSettled(),
    // NEVER AUTO-OPENS (D587, user: "the models popover should never auto
    // open. that is user only"). A model becoming resident is a CONSEQUENCE of
    // something the user already did, or of an app quietly loading one — a
    // state readout, not an event worth covering the page for. Structural
    // rather than merely unlikely: with `neverOpen`, this section has no code
    // path to `setOverride("open")` at all, so no future arrival can slip
    // through. D588 then removed this chip's INDICATOR too, so there is no
    // dot left for an arrival to set — see the chip's own comment. What
    // survives is `autoClose` (D580's Unload-the-last-row behaviour was
    // explicitly good, and closing is not announcing) and the persisted
    // preference reopening it on reload, which IS the user's own choice.
    { neverOpen: true },
  );
  // The saved preference, overridden in EITHER direction by whichever
  // transient flag is standing (D580 adds the closing half; the two are
  // mutually exclusive by construction — autoExpand.ts holds one `Override`,
  // not two independent booleans). `autoClose` is tested first because a
  // drained list beats a stale auto-open that the same drain is retiring.
  const open = autoClose ? false : !collapsed || autoOpen;

  // ONE panel at a time across the whole bar (D582). Only ever CLOSES this
  // section, and only transiently — see `exclusiveSection.ts` on why the
  // arbiter must not touch the saved preference.
  useExclusiveSection("models", open, forceClose);

  // ONE unified toggle for a chip whose visible state may be the SAVED
  // preference or either transient override (D580). It acts on what the user
  // SEES — `wantOpen = !open` — then writes the preference only if the
  // preference is what disagrees. That is what keeps D574's rule intact
  // without a special case for it: dismissing an auto-OPENED panel (or
  // reopening an auto-CLOSED one) finds the saved flag already agreeing with
  // the outcome, so clearing the override is the whole of the work and
  // nothing is persisted. A click on a chip whose state came from the
  // preference itself still flips and saves it, exactly as before.
  const toggle = () => {
    const wantOpen = !open;
    acknowledge();
    if (collapsed === wantOpen) {
      saveCollapsed(!wantOpen);
      setCollapsed(!wantOpen);
    }
  };

  // TRANSIENT ONLY — no write to the saved preference (D584 review finding 2).
  // `useDismissOnOutside` fires on any pointer-down outside THIS host, and a
  // click on a SIBLING CHIP is outside it, so the persisting version turned
  // "the user opened Models" into `jobs-collapsed = "1"` plus
  // `repo-updates-collapsed = "1"`. All three keys converged on "1" and the
  // preference became write-only — the exact "the app decided, not the user"
  // failure the D567 guard exists to prevent, arriving through the dismiss
  // path instead of through `forceClose`. So this now IS `forceClose`: the
  // panel goes away, and what the user last chose is left alone.
  const close = forceClose;

  const onUnload = async (model: string) => {
    // The response IS a fresh runtime snapshot (`{stopped, ...describe()}`,
    // ai_runtime.py's own route) — publishing it updates every reader (this
    // panel, the sidebar dot) on the click itself, rather than waiting out
    // the next poll tick.
    const result = await unloadAiModel(model);
    publishAiRuntime(result);
  };

  return (
    <ModelsCardView
      models={runtime.loaded}
      collapsed={!open}
      onToggle={toggle}
      onClose={close}
      onUnload={onUnload}
    />
  );
}
