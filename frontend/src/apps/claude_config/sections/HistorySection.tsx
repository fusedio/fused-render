// The History page — everything about the config repo AS a repo, in the order
// you care about it:
//
//   1. Uncommitted changes — the drift that exists right now, and the one
//      button that commits it. This is what the git badge pinned to the old
//      section nav used to do; the nav is gone, so the SIGNAL (a dirty dot)
//      lives on the tab strip's History button and the ACTION lives here.
//   2. Profiles — a profile IS a git branch over this same repo, so it belongs
//      with the history rather than as a tab of its own. Composed, not copied:
//      ProfilesSection stays the owner of that flow.
//   3. Commits — the log, newest first, with Restore.
//
// Restore is previewed before it happens (HEAD → target, both the file list and
// the settings.json key deltas) and is itself a FORWARD commit — history is
// never rewritten, so an unwanted restore is undone by restoring the commit
// before it. A commit here is the same deal in the other direction: it folds
// every pending edit into ONE commit, so what it is about to sweep up has to be
// visible before it happens — hence the same drift preview.
import { useCallback } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import {
  Card,
  CardActions,
  CardSub,
  CardTitle,
  Empty,
  Group,
  Pill,
  guard,
  toastErr,
  toastOk,
  useChangePreview,
  useGitStatus,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";
import ProfilesSection from "./ProfilesSection";

interface HistoryProps extends SectionProps {
  // A commit folds in drift the OTHER sections can't have accounted for, so it
  // is the panel — not this page — that decides what to remount.
  onCommitted: () => void;
}

export default function HistorySection({ onChanged, onCommitted }: HistoryProps) {
  const load = useCallback(() => cc.gitOps.log(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();
  // Epoch 0: this page is remounted by the panel whenever anything commits, so
  // there is no epoch of its own to track — only the explicit re-check.
  const { status, failed, recheck } = useGitStatus(0);

  const commit = async () => {
    const drift = await guard(cc.gitOps.drift());
    if (!drift) return;
    const choice = await ask<"commit" | false>({
      title: "Uncommitted changes",
      preview: drift,
      buttons: [
        { label: "Close", value: false },
        { label: "Commit", value: "commit", primary: true },
      ],
    });
    if (choice !== "commit") return;
    const res = await guard(cc.gitOps.commit());
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Commit failed");
      return;
    }
    toastOk("Committed");
    onCommitted();
  };

  const restore = async (sha: string) => {
    const preview = await guard(cc.gitOps.diff(sha));
    if (!preview) return;
    if (preview.error) {
      toastErr(preview.error);
      return;
    }
    const ok = await ask<boolean>({
      title: `Restore to ${sha.slice(0, 8)}?`,
      preview,
      buttons: [
        { label: "Cancel", value: false },
        { label: "Restore", value: true, primary: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.gitOps.restore(sha));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Restore failed");
      return;
    }
    toastOk("Restored");
    onChanged();
    reload();
  };

  return (
    <>
      {modal}
      <Group title="Uncommitted changes">
        <Card>
          <CardTitle>
            {failed
              ? "Status unavailable"
              : !status
                ? "Checking…"
                : status.dirty
                  ? `${status.files.length} uncommitted change(s)`
                  : "Everything is committed"}
            {status?.dirty && " "}
            {status?.dirty && <Pill tone="ro">drift</Pill>}
          </CardTitle>
          <CardSub>
            {status?.dirty
              ? "One commit folds all of them into the log below. Review first — it sweeps up every pending edit, not just the one you were making."
              : "Your Claude config on disk matches the newest commit."}
          </CardSub>
          <CardActions>
            {status?.dirty ? (
              <button type="button" className="btn btn-primary" onClick={commit}>
                Review &amp; commit
              </button>
            ) : (
              <button type="button" className="btn" disabled={failed} onClick={recheck}>
                Re-check
              </button>
            )}
          </CardActions>
        </Card>
      </Group>
      <Group title="Profiles">
        <ProfilesSection onChanged={onChanged} />
      </Group>
      <Group title="Commits">
        {error && <ErrorBanner>{error}</ErrorBanner>}
        {!data && !error && <SkeletonLines rows={5} label="Loading history" />}
        {data && !data.log.length && <Empty>No history yet.</Empty>}
        {data?.log.map((e, i) => (
          <div className="cc-log-entry" key={e.sha}>
            <span className="cc-log-msg">{e.message}</span>
            <span className="cc-log-date">{new Date(e.date).toLocaleString()}</span>
            <span className="cc-log-sha cc-mono">{e.sha.slice(0, 8)}</span>
            {i === 0 ? (
              <Pill tone="on">current</Pill>
            ) : (
              <button type="button" className="btn" onClick={() => restore(e.sha)}>
                Restore
              </button>
            )}
          </div>
        ))}
      </Group>
    </>
  );
}
