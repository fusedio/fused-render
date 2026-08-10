// History section: the config repo's git log, newest first, with Restore.
//
// Restore is previewed before it happens (HEAD → target, both the file list and
// the settings.json key deltas) and is itself a FORWARD commit — history is
// never rewritten, so an unwanted restore is undone by restoring the commit
// before it.
import { useCallback } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import { Empty, Pill, guard, toastErr, toastOk, useChangePreview, useModuleData } from "../bits";
import type { SectionProps } from "../bits";

export default function HistorySection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.gitOps.log(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();

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

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={5} label="Loading history" />;
  if (!data.log.length) return <Empty>No history yet.</Empty>;

  return (
    <>
      {modal}
      {data.log.map((e, i) => (
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
    </>
  );
}
