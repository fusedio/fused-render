// The snapshots panel's body: one box per RUN, so a chain is drawn as a chain
// (T:18731-18762, 18847-18880).
//
// GROUPED BY SESSION because the version numbers restart in every one (semantic
// 4 / SPEC FH-4): five chats that edited this file give five chains each
// beginning at v1, and a flat merged list therefore shows "v2" three times and
// reads as duplicate rows. The number is real and per-chain, so the fix is to
// draw the chain it belongs to rather than to drop or globally renumber it.
import { useState } from "react";
import { snapRuns } from "../protocol/snapshots";
import type { SnapshotsTimeline } from "../protocol/types";
import { SnapRow } from "./SnapRow";

export interface SnapshotsProps {
  agentDir: string;
  file: string;
  /** `null` = mounted and reading (the note stands in for the rows). */
  timeline: SnapshotsTimeline | null;
  /** The read failed: the note carries the reason and the heading the retry. */
  failed: boolean;
  error: string;
  /** sessionId -> the name that session's chat goes by, from the Recent list —
   *  the very same `preview` string those rows are labelled with, so a chain the
   *  user had in this app is named by what they asked for in it. A MISS is
   *  ordinary: the store records every Claude Code session that touched the
   *  file, including ones from another folder's page (T:18847-18867). */
  names?: ReadonlyMap<string, string>;
  onReloaded(timeline: SnapshotsTimeline | null, note: string): void;
  disabled?: boolean;
}

export function Snapshots({
  agentDir,
  file,
  timeline,
  failed,
  error,
  names,
  onReloaded,
  disabled,
}: SnapshotsProps) {
  // Which row is expanded is the PANEL's state and survives a repaint from a
  // revert; the expanded row is left open on purpose after a refetch, because
  // its plan is re-read against the new bytes rather than going stale.
  const [openId, setOpenId] = useState<string | null>(null);
  const [note, setNote] = useState("");

  if (failed) {
    return (
      <div className="c-snapsnote" role="alert">
        The snapshots could not be read ({error}).
      </div>
    );
  }
  if (!timeline) {
    return <div className="c-snap-note">reading the version history…</div>;
  }
  if (!timeline.available) {
    return (
      <div className="c-snapsnote">
        {timeline.note || "No Claude Code file history on this machine."}
      </div>
    );
  }

  const runs = snapRuns(timeline.versions);
  return (
    <>
      {runs.map((run, i) => (
        <div className="c-snap-runbox" key={`${run.session}-${i}`}>
          <div className="c-snap-run" title={`session ${run.session}`}>
            {/* "chat", not "another chat": on a file whose chains all come from
                outside this app that word repeats down the whole panel, and five
                headings saying "another chat" is noise pretending to be
                information. The distinguishing fact is the id, and it sits in
                the quiet half of the line beside the count (T:18869-18880). */}
            <span className="c-snap-run-name">
              {names?.get(run.session) || "chat"}
            </span>
            <span className="c-snap-run-sub">
              {(names?.get(run.session) ? "" : run.session.slice(0, 8) + " · ") +
                run.versions.length +
                (run.versions.length === 1 ? " checkpoint" : " checkpoints")}
            </span>
          </div>
          {run.versions.map((v) => (
            <SnapRow
              key={v.id}
              version={v}
              timeline={timeline}
              agentDir={agentDir}
              file={file}
              open={v.id === openId}
              onToggle={() => setOpenId(v.id === openId ? null : v.id)}
              onReverted={(next, said) => {
                setOpenId(null);
                setNote(said);
                onReloaded(next, said);
              }}
              {...(disabled ? { disabled } : {})}
            />
          ))}
        </div>
      ))}
      {note || timeline.note ? (
        <div className="c-snapsnote">{note || timeline.note}</div>
      ) : null}
    </>
  );
}
