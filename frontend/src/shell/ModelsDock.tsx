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
import StatusDot from "@platform/ui/StatusDot";
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
    // `!== "0"`, NOT `=== "1"` (D595): an ABSENT key means COLLAPSED, which is
    // every section's state on a fresh profile and was the bug — four panels
    // opened over the page at once, and the D582 arbiter then picked which one
    // survived by registration order rather than by anything meaningful. The
    // chip's circle already says whether there is anything inside, so an
    // auto-opened EMPTY panel communicates nothing and covers the page to do
    // it; "expanded is the honest default" was written when the chip carried a
    // count and the panel was the only way to see detail.
    //
    // THE STORED VALUES KEEP THEIR MEANINGS — no sentinel flip, so no
    // migration: `"1"` is still collapsed, and `"0"` is still expanded, so
    // someone who deliberately opened this section stays opened. Only the
    // absent case moves.
    return localStorage.getItem(COLLAPSED_KEY) !== "0";
  } catch {
    // Collapsed here too: a private-mode profile takes this branch on EVERY
    // load, so it is the one case that never gets to express a preference.
    return true;
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
/** THE ROW'S MEMORY FIGURES (D594, user: "lets also color code the memory
 *  usage of the model in relation to the user's total memory. if possible lets
 *  also add the real resident memory in a parenthesis next to it").
 *
 *  ORDER IS THE POINT: the model's measured COST is primary and colour-coded,
 *  the live worker RSS is parenthetical. That is what makes the pair honest —
 *  the primary answers "what does this model cost me" and the parenthetical
 *  answers "what is it holding this instant". Reversed, the figure we already
 *  agreed is inaccurate (RSS: "not the model's size") would sit in the
 *  position of authority, which is exactly why D589 took the RSS-summed
 *  aggregate off the chip.
 *
 *  NO FOOTPRINT, NO PRIMARY: a model with nothing measured and nothing
 *  declared falls back to RSS alone, UNCOLOURED — never a coloured guess and
 *  never a `0`. `title` carries the basis in the vocabulary `AiFitVerdict`
 *  already set, so a measured figure reads as fact and the other two as
 *  hedges.
 */
function MemoryCell({
  model,
  ceilingBytes,
}: {
  model: AiLoadedModel;
  ceilingBytes: number | null;
}) {
  const band = memoryBand(model.footprintBytes, ceilingBytes);
  const rss = formatSize(model.residentBytes);
  if (model.footprintBytes === null) {
    // Nothing to colour and nothing to compare — the live figure stands alone,
    // and says which figure it is so it cannot be mistaken for a cost.
    return (
      <span className="dl-amount" title="Resident memory right now">
        {rss}
      </span>
    );
  }
  const basis =
    model.footprintBasis === "measured"
      ? "Measured on this machine"
      : model.footprintBasis === "declared"
        ? "Estimated from the model's declared size"
        : "Estimated from the download size";
  return (
    <span
      className={"dl-amount" + (band ? ` is-mem-${band}` : "")}
      title={`${basis}${ceilingBytes ? ` — against ${formatSize(ceilingBytes)} usable` : ""}`}
    >
      {formatSize(model.footprintBytes)}
      {rss ? <span className="dl-mem-live"> ({rss})</span> : null}
    </span>
  );
}

/** Which colour band a model's cost falls in against this machine's ceiling
 *  (D594) — or null when there is nothing honest to colour: no footprint, or
 *  no readable ceiling.
 *
 *  THE SAME THREE STEPS `AiFitVerdict` already uses — easy / tight / no — so
 *  the status bar and the AI Models page's fit badge cannot disagree about the
 *  same model. The thresholds are utilization of the ceiling, and `no` means
 *  the model genuinely EXCEEDS it: a large model that fits is not an error,
 *  which is why `no` is the only band that gets the error colour.
 *
 *  Pure and exported so the rule is testable without rendering a row. */
export function memoryBand(
  footprintBytes: number | null,
  ceilingBytes: number | null,
): "easy" | "tight" | "no" | null {
  if (!footprintBytes || !ceilingBytes) return null;
  const used = footprintBytes / ceilingBytes;
  if (used > 1) return "no";
  return used > 0.7 ? "tight" : "easy";
}

function ModelRow({
  model,
  ceilingBytes,
  onUnload,
}: {
  model: AiLoadedModel;
  /** This machine's ceiling, for `memoryBand` — see `AiRuntime`. */
  ceilingBytes: number | null;
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
        {/* A non-ready worker holds no weights yet, so there is no cost to
            report — its STATE is the honest thing to show in this column
            instead, and the bring-up's real progress (percentage, cancel) is a
            job row in Jobs, reported by `supervisor._report` (D588). */}
        {model.state === "ready" ? (
          <MemoryCell model={model} ceilingBytes={ceilingBytes} />
        ) : (
          <span className="dl-amount">{model.state}</span>
        )}
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
  ceilingBytes = null,
  collapsed,
  onToggle,
  onClose,
  onUnload,
}: {
  models: AiLoadedModel[];
  /** This machine's memory ceiling (`AiRuntime.memoryCeilingBytes`), for the
   *  rows' colour coding. Optional and null-defaulted: with no ceiling the
   *  figures simply render uncoloured. */
  ceilingBytes?: number | null;
  collapsed: boolean;
  onToggle: () => void;
  /** Background the panel — an outside pointer-down or Escape (D574).
   *  Optional: a caller that mounts this view directly need not dismiss. */
  onClose?: () => void;
  onUnload: (model: string) => Promise<void>;
}) {
  // NO AGGREGATE MEMORY ON THE CHIP (D589, user: "the memory gb next to the
  // models isn't even accurate"). It was a sum of `residentBytes`, which
  // `api.ts`'s own comment on that field says is "RSS of the worker process.
  // Not the model's size" — so it under-reports MLX's allocator pool and
  // over-reports pages shared between workers. Summing it and labelling the
  // result as the models' memory was dishonest, and no arithmetic here could
  // fix a number that is measuring the wrong thing.
  //
  // `idle` therefore keys off the ROW LIST — is there anything to show — not
  // off a byte sum. That also dissolves D588's ready-vs-loading problem rather
  // than solving it again: with no size to fall back from, a bring-up and a
  // resident model both simply mean "there is something here". The per-row
  // `.dl-amount` KEEPS its figure: per worker it is at least a real,
  // comparable number, and it is the panel's only cost signal.
  const idle = models.length === 0;
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
        {/* THE SAME CIRCLE AS EVERY OTHER CHIP (D590, user: "lets just stick
            to a circle for all items") — reversing D588's removal of it from
            this chip alone. That removal answered a real complaint ("I see
            many different states for the model item ... this is confusing"),
            but the user's own remedy is UNIFORMITY across the bar rather than
            a per-chip judgment, and with the size gone (D589) the states this
            chip can show are now just the two every other chip has. So the
            outstanding treatments are identical everywhere: outlined vs
            filled, `.is-idle` muting, and the hover / `aria-expanded` wash. */}
        <span className="dl-summary">Models</span>
        <StatusDot
          on={models.length > 0}
          label={models.length > 0 ? "models loaded" : "no models loaded"}
        />
      </button>
      {!collapsed && (
        <div className="dl-panel">
          {idle ? (
            <div className="dl-panel-empty">No models loaded</div>
          ) : (
            <div className="dl-rows">
              {models.map((m) => (
                <ModelRow
                  key={m.model}
                  model={m}
                  ceilingBytes={ceilingBytes}
                  onUnload={onUnload}
                />
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
      ceilingBytes={runtime.memoryCeilingBytes}
      collapsed={!open}
      onToggle={toggle}
      onClose={close}
      onUnload={onUnload}
    />
  );
}
