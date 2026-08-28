// The status bar's Models section (D565): the PERSISTENT half of the three —
// what is resident in memory right now, and what it costs — left of Activity
// and Updates, which are transient work that appears and resolves (see
// StatusBar.tsx's own header comment for the lifetime-ordering principle this
// follows, the same one NotificationHost.tsx documents for its own column).
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

/** For a caller that mounts the view without an `onClose` — the pure view's
 *  outside-pointer-down handler needs a function, not a branch. */
const NOOP = () => {};
// NOTHING ABOUT THE FOLD IS PERSISTED (D603, user: "on page reload the models
// popover auto opens for some reason"). There used to be a `COLLAPSED_KEY` here
// plus `loadCollapsed`/`saveCollapsed`; all three are DELETED, not merely
// unread — a key that is written and never read is worse than no key, because
// the next reader assumes it means something.
//
// WHY: a `.dl-panel` floats above the page and is dismissed by an outside
// pointer-down or Escape. That is popover behaviour, and a popover that
// restores itself across reloads covers the page on every navigation. "Open"
// is a statement about this moment, not a preference worth remembering. The
// user's own report was not the auto-open path at all — D587's `neverOpen` was
// intact — it was a stored `"0"` from having clicked Models open earlier,
// faithfully restored on every load since, which is indistinguishable from a
// bug from where they sit. This also makes D582's arbiter trivial instead of
// arbitrary (nothing wants to be open at mount) and finally makes "never auto
// open" hold on EVERY path rather than all but one.
//
// The transient `autoOpen`/`autoClose` overrides are untouched; opening is an
// explicit click within the session. Any key left on a real machine from an
// earlier build is inert and needs no migration — nothing reads it.

// Reuses `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-amount`/`.dl-status` —
// the job row's own classes (DownloadManager.tsx) — rather than a parallel
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
 *  `osFootprintBytes` (code review 2026-08-28, finding 3, correcting a claim
 *  this docstring used to make: that RSS is a strict SUBSET of the footprint).
 *  It is not a subset in either direction. `phys_footprint` excludes clean
 *  file-backed pages and `resident_size` counts them, so a runner that maps its
 *  weights read-only — GGUF/llama.cpp, torch with `mmap=True` — has the SMALLER
 *  footprint of the two, by roughly the size of the model file. Measured in a
 *  plain interpreter with nothing loaded: 19.2 MB resident against 9.3 MB of
 *  footprint. Rendering the raw footprint there gave `8.2 GB now (1.1 GB held)`
 *  — a pair that reads as a contradiction, and a colour band (below) painted
 *  off the SMALLER number while the machine held the larger.
 *
 *  `max` RATHER THAN PICKING ONE, because neither counter is the total: RSS
 *  misses the Metal pool (172 MB of RSS against 24 GB of footprint on a live
 *  MLX FLUX worker), the footprint misses clean file pages. Their max is a
 *  strictly better LOWER BOUND than either, and it is the only cheap value that
 *  restores the invariant this pair depends on — "held" is never less than
 *  "now". `worker_base.os_footprint_bytes` now applies the same max on the
 *  measuring side, against the kernel's own `resident_size` from the same
 *  `task_vm_info` read; this one is applied again HERE because
 *  `residentBytes` is `resident_bytes()`, which can itself exceed RSS when a
 *  runner supplies its own framework probe, so only the display side can
 *  guarantee the invariant against the two numbers actually on screen.
 *
 *  THE PARENTHETICAL IS OMITTED WHEN THE TWO ARE EQUAL, which the max makes a
 *  common case rather than a rarity (every worker whose footprint is at or
 *  below its RSS, and every platform where `os_footprint_bytes` IS RSS). `1.2
 *  GB now (1.2 GB held)` carries no information and invites the reader to hunt
 *  for a difference that is not there; the bare `1.2 GB now` says the same
 *  thing. This is also what a machine with no footprint counter at all has
 *  always rendered, so the two degenerate cases now agree.
 *
 *  THE MEASURED COST IS NOT GONE — it moved into the hover `title` with its
 *  basis. The field stays in the payload because `fit.py` and the AI Models
 *  page both read it; it simply stopped competing for the row's one visible
 *  slot. See D601 for why that figure under-reports on MLX and why fixing it
 *  is deliberately not this change.
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

  // THE BAND IS COMPUTED FROM — AND PAINTED ON — THE PARENTHETICAL (D600,
  // the coordinator's one deliberate deviation from the option as worded).
  // Banding the primary would colour a 1.8 GB RSS green while the machine sat
  // at 24 GB of 24 GB: a signal that is actively false exactly when it matters
  // most, a model pinning the machine while glowing "comfortable". Against the
  // ceiling, the HELD figure is the only one on this row a colour can honestly
  // answer "how close is this to the limit" for — so the class goes on the
  // figure it DESCRIBES, never on its neighbour.
  //
  // AND IT IS `heldBytes`, NOT `model.osFootprintBytes` (finding 3): banding
  // the raw footprint painted "easy" off the smaller of two counters for every
  // mmap-heavy runner, while the machine held the larger. A band computed from
  // a number below the resident figure is the exact false comfort D600's
  // comment above says the band exists to avoid, arriving through the other
  // door.
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

  // Everything the row used to show, plus the denominator it was always judged
  // against but never named.
  const basisWord =
    model.footprintBasis === "measured"
      ? "measured on this machine"
      : model.footprintBasis === "declared"
        ? "estimated from the model's declared size"
        : "estimated from the download size";
  const title =
    [
      now ? `${now} resident right now` : null,
      // Same suppression as the visible parenthetical, for the same reason: two
      // identical byte figures in one sentence read as a mistake.
      heldAddsSomething
        ? `at least ${held} held in total, including the GPU pool`
        : null,
      cost ? `Measured cost ${cost}, ${basisWord}` : null,
      ceilingBytes ? `Machine ceiling ${formatSize(ceilingBytes)}` : null,
    ]
      .filter(Boolean)
      .join(" · ") || undefined;

  // NO FIGURE, NO COLOUR — and no default band either. `osFootprintBytes` is
  // null on any worker whose counter could not be read, which means we do not
  // know the pressure, and an uncoloured row is the correct rendering of that.
  // It must never fall back to banding the primary instead: that would keep
  // the row looking identical while silently changing what the colour MEANS.
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
 *  same input, and the two CAN therefore disagree about the same model (code
 *  review 2026-08-28, finding 12, correcting this comment's previous claim that
 *  they cannot). Since D600 the status bar bands what the worker is HOLDING
 *  right now, while the AI Models page's fit badge bands what the model COST to
 *  run — `footprintBytes`, off `fit.py`'s ladder. On MLX those differ by ~11 GB
 *  by this branch's own measurement (13 GB cost against 24 GB held, D601).
 *
 *  THAT DIVERGENCE IS THE POINT, not a bug to reconcile: the badge answers "can
 *  I run this here", a durable claim about a model, and it has to stay stable
 *  across the run for a user comparing models on a catalogue page. The bar
 *  answers "what is this costing me right now", which has to move as the worker
 *  allocates or a colour on a live readout means nothing. Sharing the THRESHOLDS
 *  is what keeps them comparable — the same utilization means the same colour —
 *  and forcing them to share an INPUT would make one of the two lie. See D601
 *  for the separate, already-scoped work on the measured cost's own
 *  under-report; nothing here changes the badge.
 *
 *  The thresholds are utilization of the ceiling, and `no` means the figure
 *  genuinely EXCEEDS it: a large model that fits is not an error, which is why
 *  `no` is the only band that gets the error colour.
 *
 *  Pure and exported so the rule is testable without rendering a row. */
export function memoryBand(
  /** The figure to band. NOT necessarily a footprint — its one call site passes
   *  the row's HELD figure, `max(residentBytes, osFootprintBytes)`, which is a
   *  live reading rather than a cost (finding 12: the parameter used to be
   *  named `footprintBytes`, which no caller has passed since D600). */
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
      {/* NAME + ACTION ONLY — the figures moved to their own line below (see
          `.dl-row-figures`'s comment in notifications.css). The head used to
          also carry `MemoryCell`/the state span, but a job like
          "FLUX.2-Klein-4B-4bit" + "1.7 GB now (2.2 GB held)" + "Unload" needs
          more width than the panel's own `min-width` cap gives a row, and
          `.dl-title`'s job-row wrap rule then broke the name at a hyphen —
          the one thing `.dl-model`'s comment says an id must never do. The
          fix is D596's own device, applied here: the row was short of LINES,
          not width.
          THIS ONLY SHRINKS A ROW WHOSE HEAD PREVIOUSLY WRAPPED — the reported
          case, and any id long enough to compete with "1.7 GB now (2.2 GB
          held)" and "Unload" for the panel's ~320px content box. A SHORT id
          like "gpt-oss-20b", whose head already fit on one line under the old
          layout, gets one line TALLER here: it trades a head that already had
          room for the figures alongside it for an always-present figures line
          below. CSS cannot pick a layout by the rendered width of its own
          content — there is no selector for "would this line have wrapped" —
          so the row does not (and structurally cannot) branch between the two
          shapes; every row pays the same one-line-head-plus-figures-line
          structure regardless of whether its own name needed it. */}
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
          job row in Jobs, reported by `supervisor._report` (D588). */}
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
          inspiration" — the bar shows the category NAME and nothing that
          varies in width, and the idle sentence moves into the panel; see
          `DownloadManagerView`'s own header comment for the fuller reasoning,
          identical here). The label is "Models" whether or not anything is
          resident — the ONLY difference between idle and active is whether the
          circle beside it is outlined or filled, plus `.is-idle`'s muting.
          The count went in D588 and the total cost in D589, so there is
          nothing left in this chip that can change its width at all. */}
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
  const [collapsed, setCollapsed] = useState(true);

  // Same wiring DownloadManagerView/RepoUpdatesCardView use, minus the half
  // this section forbids: `neverOpen` below means the only thing this hook does
  // here is CLOSE the panel when the last model unloads (D580). There is no dot
  // for an arrival either — D588 deleted `.dl-new-dot` app-wide, and the chip's
  // circle is derived from `models.length`, not from hook state. `autoOpen` is
  // never persisted regardless — see autoExpand.ts.
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
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
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
