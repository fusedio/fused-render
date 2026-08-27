// The status bar's Engines section (D591, user: "add an item for the
// background tasks (engine/daemon) thing over here too. I want the user to be
// able to stop the background engines from here").
//
// BESIDE MODELS, not beside Jobs: both report what is RUNNING RIGHT NOW, where
// Jobs and Notifications are transient work that appears and resolves — the
// lifetime ordering StatusBar.tsx's own header documents, and the order
// `exclusiveSection.ts`'s `SECTION_ORDER` now encodes.
//
// MODELLED ON ModelsDock.tsx, its closest sibling: a live-state panel with a
// per-row action and its own poll, already on the shell side of the boundary
// check, already in the exclusivity arbiter. Same pure-view / stateful-wrapper
// split, for the same reason (the view renders from a fixed list, with no
// polling and no `window`).
//
// STOPPING IS RECOVERABLE for all three kinds, which is why this offers it as
// a plain button rather than behind a confirm: a `template` engine respawns on
// the next `ensure`, a warm `app` worker on its next call (and is idle-reaped
// on a timer anyway), and a `background` daemon going down IS the documented
// "quit this app right now" action. The endpoint's own docstring
// (routers/engines.py) carries that argument so nobody later mistakes it for a
// destructive route.
import { useCallback, useEffect, useRef, useState } from "react";
import { getRunningEngines, stopEngine, type RunningEngine } from "@platform/lib/api";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import StatusDot from "@platform/ui/StatusDot";

const NOOP = () => {};
// Matches the Models section's idle cadence: this is a "what is running"
// readout, not progress, so it does not need to tick every second.
const POLL_MS = 10_000;

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

/** A useful NAME for a row, never the opaque id when something better exists:
 *  the folder's basename for a background app, the module for a warm app
 *  worker, and the id itself for a template engine — those are already
 *  readable (`map`, `geotiff`), which is why they are the fallback rather than
 *  a special case. Pure and exported so it is testable without a render. */
export function engineLabel(engine: RunningEngine): string {
  if (engine.folder) {
    const parts = engine.folder.split(/[/\\]/).filter(Boolean);
    if (parts.length > 0) return parts[parts.length - 1];
  }
  return engine.module || engine.engine_id;
}

function EngineRow({
  engine,
  onStop,
}: {
  engine: RunningEngine;
  onStop: (engineId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  // A rejected request must SAY so rather than leaving the row unchanged with
  // nothing explained — the standard D572/D566 set for exactly this shape of
  // row-scoped action, surfaced inline because the row is what the user is
  // looking at.
  const [failure, setFailure] = useState<string | null>(null);

  const stop = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await onStop(engine.engine_id);
    } catch {
      setFailure("Could not stop — check your connection and retry.");
    } finally {
      setBusy(false);
    }
  };

  // Reuses `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-amount`/`.dl-status` and
  // `.dl-row-cancel` — the job/model row vocabulary — rather than a parallel
  // `e-` set: this row is shaped exactly like a model row (a name, a
  // qualifier, one action, an optional status line), and notifications.css
  // already draws that in both themes. Stop is `.dl-row-cancel`, the same
  // "text, not a glyph" control Unload and Cancel wear.
  return (
    <div className="dl-row">
      <div className="dl-row-head">
        <span className="dl-title" title={engine.folder || engine.engine_id}>
          {engineLabel(engine)}
        </span>
        {/* The KIND, so three similarly-named rows stay distinguishable — and
            the one thing that explains why a row the user never started is
            there at all. */}
        <span className="dl-amount">{engine.kind}</span>
        <button className="dl-row-cancel" onClick={stop} disabled={busy}>
          {busy ? "Stopping…" : "Stop"}
        </button>
      </div>
      {failure && <div className="dl-status">{failure}</div>}
    </div>
  );
}

/** The pure, props-in half — see ModelsDock's own doc for why this split
 *  exists. */
export function EnginesCardView({
  engines,
  collapsed,
  onToggle,
  onClose,
  onStop,
}: {
  engines: RunningEngine[];
  collapsed: boolean;
  onToggle: () => void;
  /** Background the panel — an outside pointer-down or Escape. */
  onClose?: () => void;
  onStop: (engineId: string) => Promise<void>;
}) {
  const idle = engines.length === 0;
  const hostRef = useRef<HTMLDivElement | null>(null);
  useDismissOnOutside(hostRef, !collapsed, onClose ?? NOOP);

  return (
    <div className="dl-host" ref={hostRef}>
      <button
        className={"dl-toggle" + (idle ? " is-idle" : "")}
        onClick={onToggle}
        aria-expanded={!collapsed}
        title={collapsed ? "Show running engines" : "Hide running engines"}
      >
        {/* Label plus the bar's one shared circle (D590) — no count anywhere,
            per the user's rule: "no count. just a circle outlined or filled". */}
        <span className="dl-summary">Engines</span>
        <StatusDot
          on={engines.length > 0}
          label={engines.length > 0 ? "engines running" : "no engines running"}
        />
      </button>
      {!collapsed && (
        <div className="dl-panel">
          {idle ? (
            <div className="dl-panel-empty">No engines running</div>
          ) : (
            <div className="dl-rows">
              {engines.map((e) => (
                <EngineRow key={e.engine_id} engine={e} onStop={onStop} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function EnginesDock() {
  const [engines, setEngines] = useState<RunningEngine[]>([]);
  // Has the read answered once? An empty list is indistinguishable from "not
  // asked yet", and `useAutoExpandOnNew` needs that distinction so a page load
  // onto already-running engines is not read as a wave of arrivals
  // (autoExpand.ts's `ready`).
  const [settled, setSettled] = useState(false);
  const [collapsed, setCollapsed] = useState(true);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer = 0;
    // Only the newest invocation may schedule — the same generation guard
    // `useRepoUpdates` carries (D585 finding 7): `clearTimeout` cancels a
    // PENDING timer, but a `refresh()` landing while an earlier poll awaits
    // leaves both in flight, and each would assign `timer` on the way out,
    // leaking one unclearable chain.
    let generation = 0;
    const poll = async () => {
      const mine = ++generation;
      window.clearTimeout(timer);
      try {
        const data = await getRunningEngines();
        if (!disposed && mine === generation) {
          setEngines(data.engines || []);
          setSettled(true);
        }
      } catch {
        // Best-effort, like the other sections' polls: a failed read leaves
        // the last snapshot standing rather than blanking the list.
      }
      if (!disposed && mine === generation) timer = window.setTimeout(poll, POLL_MS);
    };
    pollRef.current = poll;
    poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);

  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    engines.map((e) => e.engine_id),
    collapsed,
    settled,
    // NEVER AUTO-OPENS, the same call the user made for Models ("the models
    // popover should never auto open. that is user only", D587): an engine
    // coming up is a consequence of opening a page or an app, not news the
    // user asked to be interrupted by. Drain-close is KEPT — stopping the last
    // engine getting the panel out of the way is the good half of D580, and
    // closing is not announcing.
    { neverOpen: true },
  );
  const open = autoClose ? false : !collapsed || autoOpen;

  useExclusiveSection("engines", open, forceClose);

  const toggle = () => {
    const wantOpen = !open;
    acknowledge();
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
  };

  const close = forceClose;

  const onStop = async (engineId: string) => {
    await stopEngine(engineId);
    // Optimistic, then let the poll be the truth — the row goes on the
    // server's confirmation rather than up to POLL_MS later, and a refresh is
    // asked for immediately so a failed teardown reappears.
    setEngines((list) => list.filter((e) => e.engine_id !== engineId));
    refresh();
  };

  return (
    <EnginesCardView
      engines={engines}
      collapsed={!open}
      onToggle={toggle}
      onClose={close}
      onStop={onStop}
    />
  );
}
