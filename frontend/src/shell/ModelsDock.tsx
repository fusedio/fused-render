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
import { useRef, useState } from "react";
import { unloadAiModel, type AiLoadedModel } from "@platform/lib/api";
import { formatSize, repoName } from "@platform/lib/format";
import { aiRuntimeSettled, publishAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import StatusDot from "@platform/ui/StatusDot";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";

/** For a caller that mounts the view without an `onClose` — the pure view's
 *  outside-pointer-down handler needs a function, not a branch. */
const NOOP = () => {};
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
  // THE RAM/VRAM SPLIT (D670): known exactly when a runner reported BOTH a
  // host RSS reading and at least one device-allocator reading — a device
  // figure with no host reading falls through to the unsplit rendering below
  // rather than inventing a RAM figure this row cannot back up (mirrors
  // supervisor._worker_footprint_bytes's own fallback: unknown must never
  // look like zero). A host reading with no device figure is the common case
  // (CPU, mmap'd GGUF/llama.cpp, unified-memory MLX/mflux and torch-on-MPS)
  // and is not a split at all — there is nothing to hold apart, and the
  // unsplit rendering below is exactly correct for it.
  if (
    model.hostResidentBytes !== null &&
    (model.deviceAllocatedBytes !== null || model.deviceReservedBytes !== null)
  ) {
    return <SplitMemoryCell model={model} ceilingBytes={ceilingBytes} />;
  }

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

/** THE SPLIT ROW (D670): a discrete GPU's own pool reported apart from host
 *  RAM — `worker_base.host_resident_bytes`/`device_memory_bytes` on the
 *  worker side, `Worker.hostResidentBytes`/`deviceAllocatedBytes`/
 *  `deviceReservedBytes` here. `MemoryCell` routes here only once BOTH a host
 *  and a device reading are in hand; see its own docstring for why the two
 *  gates differ.
 *
 *  RAM leads, unparenthesized, mirroring `MemoryCell`'s own "now"/"held"
 *  pair one level up: `2.5 GB RAM · 5.2 GB VRAM (8.1 GB held)`. VRAM gets the
 *  same now/held pairing `MemoryCell` gives RSS/footprint — allocated is
 *  what the model is using now, reserved is what the driver is holding for
 *  it, `max` keeps "held" never less than "now", and the parenthetical is
 *  dropped when the two agree, same rule as above.
 *
 *  BANDED ON RAM ONLY: `supervisor._worker_footprint_bytes` charges the host
 *  figure against the RAM budget once the split is known — banding VRAM
 *  against a RAM ceiling would reintroduce, in the chip, the exact
 *  conflation this split exists to remove from the gate. */
function SplitMemoryCell({
  model,
  ceilingBytes,
}: {
  model: AiLoadedModel;
  ceilingBytes: number | null;
}) {
  const ram = formatSize(model.hostResidentBytes);
  const vramNowBytes = model.deviceAllocatedBytes;
  const vramHeldBytes =
    model.deviceReservedBytes === null
      ? null
      : Math.max(model.deviceReservedBytes, model.deviceAllocatedBytes ?? 0);
  const vramNow = formatSize(vramNowBytes);
  const vramHeld = formatSize(vramHeldBytes);
  const vramHeldAddsSomething =
    vramHeldBytes !== null && vramHeldBytes > (vramNowBytes ?? 0);

  const band = memoryBand(model.hostResidentBytes, ceilingBytes);
  const bandClass = band ? ` is-mem-${band}` : "";

  const basisWord =
    model.footprintBasis === "measured"
      ? "measured on this machine"
      : model.footprintBasis === "declared"
        ? "estimated from the model's declared size"
        : "estimated from the download size";
  const cost = formatSize(model.footprintBytes);
  const title =
    [
      ram ? `${ram} host RAM resident right now` : null,
      vramNow ? `${vramNow} allocated on the GPU` : null,
      vramHeldAddsSomething ? `at least ${vramHeld} reserved on the GPU` : null,
      cost ? `Measured cost ${cost}, ${basisWord}` : null,
      ceilingBytes ? `Machine ceiling ${formatSize(ceilingBytes)}` : null,
    ]
      .filter(Boolean)
      .join(" · ") || undefined;

  // NO VRAM READING, NO CLAUSE — `deviceAllocatedBytes` can be null while
  // `deviceReservedBytes` alone is known (or vice versa); the row shows
  // whichever half it has rather than a stranded "0 B".
  const vramClause = vramNow ? (
    <>
      {" · "}
      {vramNow} VRAM
      {vramHeldAddsSomething ? <> ({vramHeld} held)</> : null}
    </>
  ) : null;

  return (
    <span className="dl-amount" title={title}>
      <span className={"dl-mem-live" + bandClass}>{ram} RAM</span>
      {vramClause}
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
  // off a byte sum. The per-row `.dl-amount` KEEPS its figure: per worker it
  // is at least a real, comparable number, and it is the panel's only cost
  // signal. `models.length > 0` is also this chip's `StatusDot` fill rule —
  // the whole reason this section is its own chip again: the dot must fill
  // whenever ANY model is resident, whether or not any job or engine is also
  // running, and folding this into a shared "is there work right now" dot
  // (as the merged Activity chip's did) answers a different question than
  // the one the user relies on this dot for.
  const idle = models.length === 0;
  // Wraps the chip AND the panel — dismissOnOutside.ts explains why the whole
  // host, not just the panel, is what counts as "inside".
  const hostRef = useRef<HTMLDivElement | null>(null);
  useDismissOnOutside(hostRef, !collapsed, onClose ?? NOOP);

  return (
    <div className="dl-host" ref={hostRef}>
      {/* ALWAYS a real, clickable button now (D573, user: "the chevron
          doesn't belong to the status bar. lets follow vscode/cursor for
          inspiration" — the bar shows the category NAME and nothing that
          varies in width, and the idle sentence moves into the panel; see
          `DownloadManagerView`'s own header for why a resident model or a
          running engine alone leaves ITS dot unfilled — that rule does not
          apply here). The label is "Models" whether or not anything is
          resident — the ONLY difference between idle and active is whether the
          circle beside it is outlined or filled, plus `.is-idle`'s muting. */}
      <button
        className={"dl-toggle" + (idle ? " is-idle" : "")}
        onClick={onToggle}
        aria-expanded={!collapsed}
        title={collapsed ? "Show loaded models" : "Hide loaded models"}
      >
        {/* THE SAME CIRCLE AS EVERY OTHER CHIP (D590, user: "lets just stick
            to a circle for all items"). Filled whenever ANY model is
            resident — the one signal the user calls out by name as the
            reason this chip must stand apart from Activity's own dot (which
            answers "is there work right now", not "is memory held"). */}
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
  const [collapsed, setCollapsed] = useState(true);

  // Same wiring DownloadManagerView/RepoUpdatesCardView use, with `ids`
  // deliberately fed nothing (D587, user: "the models popover should never
  // auto open. that is user only") — a model becoming resident is a
  // CONSEQUENCE of something the user already did, or of an app quietly
  // loading one — a state readout, not an event worth covering the page for.
  // `autoExpand.ts` deleted the dedicated `neverOpen` flag the pre-merge
  // version of this file used to pass here (nothing outside the status-bar
  // merge's now-deleted standalone Models/Engines chips ever wanted it, so
  // the merge removed it along with them); the identical effect is available
  // through the hook's general shape instead — an empty `ids` array can never
  // contain an "arrival", so `autoOpen` can never become true, structurally,
  // with no separate flag needed. Every resident model still rides in as
  // `alsoDrawn`, which is what lets D580's "close when the last model
  // unloads" behaviour keep working: `autoClose` reacts to the DRAWN set
  // going from non-empty to empty, and `alsoDrawn` is exactly that set here.
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    [],
    collapsed,
    // Not `runtime.loaded.length > 0` — an idle machine must still announce
    // its first real load (autoExpand.ts's `ready`), which matters for
    // `autoClose` even though `autoOpen` can never fire here.
    aiRuntimeSettled(),
    { alsoDrawn: runtime.loaded.map((m) => `model:${m.model}`) },
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
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
  };

  // TRANSIENT ONLY — no write to the saved preference (D584 review finding 2).
  // `useDismissOnOutside` fires on any pointer-down outside THIS host, and a
  // click on a SIBLING CHIP is outside it, so a persisting version would turn
  // "the user opened Models" into a write on every OTHER section's own key —
  // the exact "the app decided, not the user" failure the D567 guard exists
  // to prevent, arriving through the dismiss path instead of through
  // `forceClose`. So this now IS `forceClose`: the panel goes away, and what
  // the user last chose is left alone.
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
