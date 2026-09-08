// The status bar's Models section (D565): the PERSISTENT half — what is
// resident in memory right now, and what it costs — its own chip again after
// a brief detour: the status-bar merge (commit 33fc407d) folded this into the
// combined "Activity" chip alongside Jobs and Engines, but the user relies on
// this chip's own filled/outlined dot to know whether the machine is holding
// any model weights at all, and that signal got diluted once the dot also had
// to answer for jobs and engines. This file is Models' chip again, left of
// Activity and Notifications (StatusBar.tsx's own header comment has the
// full lifetime-ordering principle: Models is persistent status, Jobs/
// Notifications are transient work that appears and resolves).
//
// A QUICK-INFO POPOVER, NOT A MANAGEMENT CONSOLE (user call, revising an
// earlier gauge/progress-bar draft: "we don't need a gauge if too
// complicated. just a quick info upon clicking which we have a list of
// loaded models we can unload"). The chip is the label `Models` plus ONE
// circle, outlined when nothing is resident and filled when something is
// (D588/D590, user: "no count. just a circle outlined or filled") — it carried
// a count until D588 and a total cost until D589, and carries neither now. The
// panel is one row per resident model: its name, what it is holding, and an
// Unload button. No gauge, no proportional fill, no RAM fraction — that is the
// whole feature.
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
// this section and hands it to `StatusBar` as `models`, same as before the
// merge.
//
// `ModelRow`/`MemoryCell`/`memoryBand` are NOT shared with
// `platform/ui/DownloadManager.tsx` any more — they moved there and back
// during the merge/split, and each now has its own copy shaped for its own
// row family (a model row's figures are a two-line memory readout with an
// Unload button, unrelated to a job row's progress bar or an engine row's
// Stop button). Duplicating this little rather than threading a shared
// export between shell and platform keeps `check-boundaries.mjs` simple: this
// file only needs `AiLoadedModel` (platform/lib/api, a type) and
// `unloadAiModel` (platform/lib/api, a call), nothing shell-only leaks into
// platform.
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
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";

// NOTHING ABOUT THE FOLD IS PERSISTED (D603, user: "on page reload the models
// popover auto opens for some reason"). There used to be a `COLLAPSED_KEY` here
// plus `loadCollapsed`/`saveCollapsed`; all three stay deleted — a key that is
// written and never read is worse than no key, because the next reader assumes
// it means something.
//
// WHY: a `.dl-panel` floats above the page and is dismissed by an outside
// pointer-down or Escape. That is popover behaviour, and a popover that
// restores itself across reloads covers the page on every navigation. "Open"
// is a statement about this moment, not a preference worth remembering.
//
// The transient `autoOpen`/`autoClose` overrides are untouched; opening is an
// explicit click within the session.

// Reuses `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-amount`/`.dl-status` —
// the job row's own classes (notifications.css) — rather than a parallel
// `m-` set: a model row is shaped exactly like a job row (a name, a number,
// an action, an optional status line under it), and notifications.css
// already draws that shape correctly in both themes. `.dl-row-cancel` is
// Unload's, the same "text, not a ✕" control JobRow's own Cancel wears
// (round 1: two controls, one meaning each) — Unload is a distinct verb
// from Cancel, but the same visual language: a row-scoped, quietly-styled
// text button, not a glyph.
/** THE ROW'S MEMORY FIGURES. Both are INSTANTANEOUS now, and one is a genuine
 *  subset of the other, which is what makes the pair readable at all (D600 —
 *  the user's own pick from three spelled-out options): `1.8 GB now (24 GB
 *  held)`.
 *
 *  `residentBytes` LEADS and the held figure is parenthetical, the reverse
 *  of D594/D597 where the primary was the measured COST. Pairing a durable
 *  cost with a live reading put two numbers on different time axes side by
 *  side, and on MLX they disagreed by ~11 GB for no visible reason (13 GB cost
 *  against 24 GB held). Both figures here are "right now", which is what makes
 *  the pair readable at all.
 *
 *  THE HELD FIGURE IS `max(residentBytes, osFootprintBytes)`, NOT
 *  `osFootprintBytes`: it is not a subset in either direction. `phys_footprint`
 *  excludes clean file-backed pages and `resident_size` counts them, so a
 *  runner that maps its weights read-only — GGUF/llama.cpp, torch with
 *  `mmap=True` — has the SMALLER footprint of the two, by roughly the size of
 *  the model file. `max` is a strictly better LOWER BOUND than either, and the
 *  only cheap value that keeps the invariant this pair depends on — "held" is
 *  never less than "now".
 *
 *  THE PARENTHETICAL IS OMITTED WHEN THE TWO ARE EQUAL, which the max makes a
 *  common case rather than a rarity. `1.2 GB now (1.2 GB held)` carries no
 *  information and invites the reader to hunt for a difference that is not
 *  there; the bare `1.2 GB now` says the same thing.
 *
 *  THE MEASURED COST IS NOT GONE — it moved into the hover `title` with its
 *  basis. The field stays in the payload because `fit.py` and the AI Models
 *  page both read it; it simply stopped competing for the row's one visible
 *  slot.
 *
 *  LABELLED IN WORDS (`now` / `held`): two byte figures side by side are
 *  otherwise indistinguishable, and the entire point of this pairing is that
 *  they answer different questions.
 */
function MemoryCell({
  model,
  ceilingBytes,
}: {
  model: AiLoadedModel;
  ceilingBytes: number | null;
}) {
  // THE HELD FIGURE, `max`ed against the resident one — this docstring's own
  // section on why. `null` stays `null`: no counter means we do not know, and
  // the row must not invent a held figure out of RSS alone.
  const heldBytes =
    model.osFootprintBytes === null
      ? null
      : Math.max(model.osFootprintBytes, model.residentBytes ?? 0);

  // THE BAND IS COMPUTED FROM — AND PAINTED ON — THE PARENTHETICAL (D600).
  // Banding the primary would colour a 1.8 GB RSS green while the machine sat
  // at 24 GB of 24 GB: a signal that is actively false exactly when it matters
  // most. Against the ceiling, the HELD figure is the only one on this row a
  // colour can honestly answer "how close is this to the limit" for.
  const band = memoryBand(heldBytes, ceilingBytes);
  const bandClass = band ? ` is-mem-${band}` : "";
  const now = formatSize(model.residentBytes);
  const held = formatSize(heldBytes);
  const cost = formatSize(model.footprintBytes);
  // NOTHING TO SAY when the two figures agree — see the docstring. Compared as
  // BYTES, not as formatted strings: `formatSize` rounds, so two genuinely
  // different figures can print alike, and suppressing on the strings would
  // hide a real difference the band is still being computed from.
  const heldAddsSomething = heldBytes !== null && heldBytes > (model.residentBytes ?? 0);

  const basisWord =
    model.footprintBasis === "measured"
      ? "measured on this machine"
      : model.footprintBasis === "declared"
        ? "estimated from the model's declared size"
        : "estimated from the download size";
  const title =
    [
      now ? `${now} resident right now` : null,
      heldAddsSomething ? `at least ${held} held in total, including the GPU pool` : null,
      cost ? `Measured cost ${cost}, ${basisWord}` : null,
      ceilingBytes ? `Machine ceiling ${formatSize(ceilingBytes)}` : null,
    ]
      .filter(Boolean)
      .join(" · ") || undefined;

  // NO FIGURE, NO COLOUR — and no default band either. `osFootprintBytes` is
  // null on any worker whose counter could not be read, which means we do not
  // know the pressure, and an uncoloured row is the correct rendering of that.
  const heldCell = held ? (
    <span className={"dl-mem-live" + bandClass}>{held} held</span>
  ) : null;

  // Three shapes rather than one clever expression, because the degenerate
  // cases are where a memory cell tells its worst lies: a lone "0", or a
  // stranded "(… held)" with nothing in front of it.
  if (!now) {
    return (
      <span className="dl-amount" title={title}>
        {heldCell}
      </span>
    );
  }
  return (
    <span className="dl-amount" title={title}>
      {`${now} now`}
      {heldAddsSomething ? <> ({heldCell})</> : null}
    </span>
  );
}

/** Which colour band a byte figure falls in against this machine's ceiling
 *  (D594) — or null when there is nothing honest to colour: no figure, or no
 *  readable ceiling.
 *
 *  THE SAME THREE STEPS `AiFitVerdict` uses — easy / tight / no — but NOT the
 *  same input: since D600 this bands what the worker is HOLDING right now,
 *  while the AI Models page's fit badge bands what the model COST to run
 *  (`footprintBytes`, off `fit.py`'s ladder). On MLX those differ by ~11 GB by
 *  measurement (13 GB cost against 24 GB held, D601).
 *
 *  THAT DIVERGENCE IS THE POINT: the badge answers "can I run this here", a
 *  durable claim about a model; the bar answers "what is this costing me
 *  right now", which has to move as the worker allocates. Sharing the
 *  THRESHOLDS is what keeps them comparable — the same utilization means the
 *  same colour — and forcing them to share an INPUT would make one of the two
 *  lie.
 *
 *  The thresholds are utilization of the ceiling, and `no` means the figure
 *  genuinely EXCEEDS it: a large model that fits is not an error, which is why
 *  `no` is the only band that gets the error colour.
 *
 *  Pure and exported so the rule is testable without rendering a row. */
export function memoryBand(
  /** The figure to band. NOT necessarily a footprint — its one call site
   *  passes the row's HELD figure, `max(residentBytes, osFootprintBytes)`,
   *  which is a live reading rather than a cost. */
  bytes: number | null,
  ceilingBytes: number | null,
): "easy" | "tight" | "no" | null {
  if (!bytes || !ceilingBytes) return null;
  const used = bytes / ceilingBytes;
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
      {/* NAME + ACTION ONLY — the figures are on their own line below (see
          `.dl-row-figures`'s comment in notifications.css). */}
      <div className="dl-row-head">
        {/* The MODEL name only, not the whole `owner/model` repo id — the
            same trim `.dl-model` uses on the job row, for the same reason:
            the owner never distinguishes anything and eats width a narrow
            bar has none of. Full id stays on hover.
            `dl-title-id` (notifications.css) is what keeps this on ONE line
            instead of inheriting `.dl-title`'s two-line, wrap-anywhere job-row
            rule — a model id is a single unbreakable token, not a prompt. */}
        <span className="dl-title dl-title-id" title={model.model}>
          {repoName(model.model)}
        </span>
        <button className="dl-row-cancel" onClick={unload} disabled={busy}>
          {busy ? "Unloading…" : "Unload"}
        </button>
      </div>
      {/* The figures, on their own line — `.dl-row-figures` in
          notifications.css. A non-ready worker holds no weights yet, so there
          is no cost to report — its STATE is the honest thing to show here
          instead, and the bring-up's real progress (percentage, cancel) is a
          job row in Jobs/Activity, reported by `supervisor._report` (D588). */}
      <div className="dl-row-figures">
        {model.state === "ready" ? (
          <MemoryCell model={model} ceilingBytes={ceilingBytes} />
        ) : (
          <span className="dl-amount">{model.state}</span>
        )}
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
  pinned = false,
  hostProps,
  onUnload,
}: {
  models: AiLoadedModel[];
  /** This machine's memory ceiling (`AiRuntime.memoryCeilingBytes`), for the
   *  rows' colour coding. Optional and null-defaulted: with no ceiling the
   *  figures simply render uncoloured. */
  ceilingBytes?: number | null;
  collapsed: boolean;
  onToggle: () => void;
  /** Held open by a click (statusbar redesign) — styles the chip as engaged. */
  pinned?: boolean;
  /** Hover intent + outside-dismiss wiring for the `.dl-host` wrapper, from
   *  `useStatusChip`. Optional: a test that mounts the view bare needs none. */
  hostProps?: StatusChipState["hostProps"];
  onUnload: (model: string) => Promise<void>;
}) {
  // NO AGGREGATE MEMORY ON THE CHIP (D589, user: "the memory gb next to the
  // models isn't even accurate"): `residentBytes` is worker RSS, not model
  // size, so no sum of it is honest. The per-row `.dl-amount` keeps its figure.
  //
  // THE CHIP READS (statusbar redesign):
  //   0 models  "Models", muted
  //   1 model   the model's own short name — the bar has room for it, and the
  //             name is what you actually want to know; a model still loading
  //             carries the indeterminate sweep under it
  //   2+ models "Models 2"
  const idle = models.length === 0;
  const only = models.length === 1 ? models[0] : null;
  const label = only ? repoName(only.model) : "Models";
  const count = models.length >= 2 ? models.length : 0;
  const progress = only && only.state !== "ready" && only.state !== "error" ? null : undefined;
  const ariaLabel = idle
    ? "Models, none loaded"
    : only
      ? `Model loaded: ${repoName(only.model)}`
      : `Models, ${models.length} loaded`;

  return (
    <div className="dl-host" {...hostProps}>
      <StatusChip
        label={label}
        count={count}
        tone={idle ? "idle" : "on"}
        progress={progress}
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? "Show loaded models" : "Hide loaded models"}
        ariaLabel={ariaLabel}
        onClick={onToggle}
      />
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

// NEVER AUTO-OPENS (D587, and now the rule for every chip — statusbar
// redesign): a model finishing its load is announced by the chip's own label
// and the sweep ending, not by a panel appearing over the page. Hover to
// preview, click to pin; `lib/statusChip.ts` owns those rules.
export default function ModelsDock() {
  const runtime = useAiRuntime();
  const chip = useStatusChip("models");

  const onUnload = async (model: string) => {
    const result = await unloadAiModel(model);
    publishAiRuntime(result);
  };

  return (
    <ModelsCardView
      models={runtime.loaded}
      ceilingBytes={runtime.memoryCeilingBytes}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
      onUnload={onUnload}
    />
  );
}
