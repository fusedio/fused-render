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
import { useState } from "react";
import { unloadAiModel, type AiLoadedModel } from "@platform/lib/api";
import { formatSize, repoName } from "@platform/lib/format";
import { publishAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";

// This section's own persisted collapse preference — a THIRD independent key
// beside `fused-render:jobs-collapsed` and `fused-render:repo-updates-collapsed`
// (DownloadManager.tsx / RepoUpdatesDock.tsx), for the identical reason those
// two are separate: three sections with three separate histories a user
// might want folded independently.
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
        <span className="dl-amount">{formatSize(model.residentBytes)}</span>
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
  totalResidentBytes,
  collapsed,
  hasNew,
  onToggle,
  onUnload,
}: {
  models: AiLoadedModel[];
  totalResidentBytes: number | null;
  collapsed: boolean;
  /** An unacknowledged model loaded while collapsed — a quiet dot, never a
   *  forced expansion (`lib/autoExpand.ts`'s own doc, code review finding
   *  #4). */
  hasNew: boolean;
  onToggle: () => void;
  onUnload: (model: string) => Promise<void>;
}) {
  const idle = models.length === 0;

  return (
    <div className="dl-host">
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
        <span className="dl-summary">
          {idle
            ? "Models"
            : `Models ${models.length}${totalResidentBytes ? ` · ${formatSize(totalResidentBytes)}` : ""}`}
        </span>
        {hasNew && <span className="dl-new-dot" aria-hidden="true" />}
      </button>
      {!collapsed && (
        <div className="dl-panel">
          {idle ? (
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

  // Same wiring DownloadManagerView/RepoUpdatesCardView use — a quiet dot
  // for a model that loaded while this chip was collapsed, never a forced
  // expansion.
  const hasNew = useAutoExpandOnNew(
    runtime.loaded.map((m) => m.model),
    collapsed,
  );

  const toggle = () => {
    setCollapsed((was) => {
      saveCollapsed(!was);
      return !was;
    });
  };

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
      totalResidentBytes={runtime.totalResidentBytes}
      collapsed={collapsed}
      hasNew={hasNew}
      onToggle={toggle}
      onUnload={onUnload}
    />
  );
}
