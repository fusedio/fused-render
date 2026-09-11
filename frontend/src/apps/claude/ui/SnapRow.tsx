// One checkpoint, and what going back to it would do (T:18763-19026).
//
// The row and its expansion share ONE box, so an open body is visibly part of
// the row it belongs to rather than a panel floating under the list.
//
// The plan is fetched fresh on every expansion and never cached: it is a
// statement about the file as it is RIGHT NOW, and a stale one is exactly how a
// user confirms one diff and gets a different one. A refusal is rendered IN the
// row — the row is a real thing that exists, and hiding it would read as a bug
// rather than as an answer.
import { useEffect, useState } from "react";
import {
  snapAgo,
  snapDeltaLabel,
  snapVersionLabel,
  snapshotPlan,
  snapshotRevert,
} from "../protocol/snapshots";
import type {
  SnapshotPlanOk,
  SnapshotVersion,
  SnapshotsTimeline,
} from "../protocol/types";

const TONE: Record<string, string> = {
  plus: "c-snap-plus",
  minus: "c-snap-minus",
  plain: "",
};

export interface SnapRowProps {
  version: SnapshotVersion;
  /** The whole timeline, for `position` — which row is what is on disk now. */
  timeline: SnapshotsTimeline;
  agentDir: string;
  file: string;
  open: boolean;
  onToggle(): void;
  /** A revert landed. `timeline` is the post-write one when the write handed
   *  one back; otherwise the caller re-reads. */
  onReverted(timeline: SnapshotsTimeline | null, note: string): void;
  disabled?: boolean;
}

export function SnapRow({
  version,
  timeline,
  agentDir,
  file,
  open,
  onToggle,
  onReverted,
  disabled,
}: SnapRowProps) {
  const here = version.id === timeline.position;
  const toggle = () => {
    if (disabled) return;
    onToggle();
  };
  return (
    <div className="c-snap-item">
      <div
        className={`c-snap-row${here ? " is-here" : ""}${open ? " is-open" : ""}`}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        {...(here ? { title: "what is on disk now" } : {})}
        onClick={toggle}
        onKeyDown={(ev) => {
          if (ev.key !== "Enter" && ev.key !== " ") return;
          ev.preventDefault();
          toggle();
        }}
      >
        {/* The dot answers exactly ONE question — where is disk right now
            (T:18779-18781). */}
        <span className="c-snap-dot" aria-hidden="true">
          {here ? "●" : "○"}
        </span>
        <span className="c-snap-v">{snapVersionLabel(version)}</span>
        <span
          className="c-snap-when"
          title={
            (version.mtime
              ? new Date(version.mtime * 1000).toLocaleString() + " · "
              : "") + `session ${version.session}`
          }
        >
          {snapAgo(version.mtime)}
        </span>
        <span className="c-snap-d">
          {snapDeltaLabel(version).map((part, i) => (
            <span key={i} className={TONE[part.tone]}>
              {part.text}
            </span>
          ))}
        </span>
        <span className="c-snap-caret" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </div>
      {open ? (
        <SnapBody
          version={version}
          agentDir={agentDir}
          file={file}
          onReverted={onReverted}
        />
      ) : null}
    </div>
  );
}

function SnapBody({
  version,
  agentDir,
  file,
  onReverted,
}: {
  version: SnapshotVersion;
  agentDir: string;
  file: string;
  onReverted(timeline: SnapshotsTimeline | null, note: string): void;
}) {
  const [plan, setPlan] = useState<SnapshotPlanOk | null>(null);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(true);

  useEffect(() => {
    let live = true;
    setReading(true);
    setPlan(null);
    setError("");
    void snapshotPlan(agentDir, file, version.id)
      .then((out) => {
        if (!live) return;
        setReading(false);
        if ("ok" in out && out.ok) {
          setPlan(out);
          return;
        }
        setError(
          ("error" in out && out.error) || "this snapshot cannot be restored",
        );
      })
      .catch((err: unknown) => {
        if (!live) return;
        setReading(false);
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, [agentDir, file, version.id]);

  return (
    <div className="c-snap-body">
      {reading ? (
        <div className="c-snap-note">reading this snapshot…</div>
      ) : null}
      {error ? (
        <div className="c-snap-err" role="alert">
          {error}
        </div>
      ) : null}
      {plan ? (
        <>
          <SnapDiff plan={plan} />
          <SnapAction
            plan={plan}
            agentDir={agentDir}
            file={file}
            onReverted={onReverted}
          />
        </>
      ) : null}
    </div>
  );
}

/** The change itself: the counts on the row answer how MUCH and never WHAT, and
 *  on the one destructive action here the second is the question being
 *  confirmed. `reason` is the helper's single channel for every "no diff", so a
 *  missing diff always says why (T:18932-18962). */
function SnapDiff({ plan }: { plan: SnapshotPlanOk }) {
  const d = plan.diff;
  if (!d || !d.lines || !d.lines.length) {
    return (
      <div className="c-snap-note">
        {d?.reason || "There is no diff to show for this snapshot."}
      </div>
    );
  }
  return (
    <>
      <pre className="c-snap-diff">
        {d.lines.map((ln, i) => (
          <span key={i} className={diffClass(ln)}>
            {ln + "\n"}
          </span>
        ))}
      </pre>
      {d.truncated ? (
        <div className="c-snap-note">
          Showing part of {d.changed} changed lines.
        </div>
      ) : null}
    </>
  );
}

/** The `---`/`+++` file headers are tested BEFORE `+`/`-`: they open with the
 *  same characters, and colouring them as added and removed lines is how a
 *  two-line header reads as part of the change (T:18948-18953). */
function diffClass(ln: string): string {
  if (ln.startsWith("---") || ln.startsWith("+++")) return "c-dl-head";
  if (ln.startsWith("@@")) return "c-dl-hunk";
  if (ln.startsWith("+")) return "c-dl-add";
  if (ln.startsWith("-")) return "c-dl-del";
  return "";
}

/** ONE button, armed by a click rather than acting on it. The confirm is inline
 *  so the diff being confirmed stays on screen underneath it, and the cost is
 *  stated BEFORE the click: when the bytes on disk are in no snapshot, this
 *  write destroys the only copy (T:18964-19000). */
function SnapAction({
  plan,
  agentDir,
  file,
  onReverted,
}: {
  plan: SnapshotPlanOk;
  agentDir: string;
  file: string;
  onReverted(timeline: SnapshotsTimeline | null, note: string): void;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const gone = plan.action === "delete";
  // No kind noun anywhere in this copy: the panel is files-only, but a literal
  // "file" in a spoken string is the sink this page has regressed on three
  // times (test_claude_kind), so the wording makes no claim (T:18974-18978).
  const risk = plan.unique_current
    ? "What is on disk now is in no snapshot — going back destroys the only copy of it."
    : "";

  const go = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const out = await snapshotRevert(agentDir, file, plan);
      if ("error" in out && out.error) throw new Error(out.error);
      const ok = "ok" in out && out.ok ? out : null;
      onReverted(
        ok?.timeline ?? null,
        ok?.action === "delete"
          ? "Removed — that is how it was at this snapshot."
          : "Back at this snapshot.",
      );
    } catch (err) {
      setBusy(false);
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (error) {
    return (
      <div className="c-snap-act">
        <span className="c-snap-err" role="alert">
          {error}
        </span>
      </div>
    );
  }
  if (busy) {
    return (
      <div className="c-snap-act">
        <span className="c-snap-note">going back…</span>
      </div>
    );
  }
  return (
    <div className="c-snap-act">
      {armed ? (
        <>
          <span className="c-snap-ask">
            {gone ? "Really delete it?" : "Really go back?"}
          </span>
          <button
            type="button"
            className="c-snap-go"
            onClick={() => void go()}
          >
            Yes
          </button>
          <button
            type="button"
            className="c-snap-no"
            onClick={() => setArmed(false)}
          >
            No
          </button>
        </>
      ) : (
        <button
          type="button"
          className="c-snap-go"
          onClick={() => setArmed(true)}
        >
          {gone ? "Go back to before it existed" : "Go back to this snapshot"}
        </button>
      )}
      {risk ? <span className="c-snap-note">{risk}</span> : null}
    </div>
  );
}
