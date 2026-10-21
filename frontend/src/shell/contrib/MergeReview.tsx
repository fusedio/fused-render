// Claude's merge, as the user watches and decides it: the elapsed clock while
// it runs, then Keep the merge / Discard the merge once it is ready. One
// component for the two places a merge job shows — the Source panel (this
// copy's own merge: updating, returning, own-sync) and a Proposals-tab card
// (a proposal that no longer fits, merged onto the app's latest). Keep and
// Discard are the merge job's own routes (/merge/accept, /merge/discard); the
// server routes a proposal's Keep onto the sender's proposal itself.
//
// `brief`: the Source panel's sentence already says what Claude is doing or
// did, with its summary, and its files are the merge's — so the block adds
// only the clock or the choice. A card has no such sentence: it says all of
// it here.
import { useEffect, useRef, useState } from "react";
import { Loader2, TriangleAlert } from "lucide-react";
import { acceptMerge, discardMerge, plainMessage, type MergeJobView } from "@platform/lib/contrib";
import { notify } from "@platform/lib/notifications";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { ClaudeMark } from "@platform/ui/ClaudeMark";
import { QuietAction } from "./StatusStrip";
import { elapsed } from "./words";

/** A merge worth drawing: in flight, waiting for the user's choice, or (on
 *  a card) failed. */
export function mergeShown(job: MergeJobView | null | undefined, withFailed = false): job is MergeJobView {
  if (!job) return false;
  const s = job.status;
  return s === "running" || s === "landing" || s === "ready" || (withFailed && s === "failed");
}

export function MergeReview({
  dir,
  job,
  brief,
  disabled,
  refresh,
}: {
  dir: string;
  job: MergeJobView;
  brief?: boolean;
  /** Another action on the same surface is running. */
  disabled?: boolean;
  refresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<null | "keep" | "discard">(null);
  const [error, setError] = useState<string | null>(null);
  const clock = useMergeClock(job);

  const run = async (key: "keep" | "discard") => {
    if (busy) return;
    setBusy(key);
    setError(null);
    const r = key === "keep" ? await acceptMerge(dir, job.id) : await discardMerge(dir, job.id);
    if (!r.ok) setError(plainMessage(r));
    else if (r.message) notify({ title: r.message, tone: "info" });
    await refresh();
    setBusy(null);
  };

  if (job.status === "running" || job.status === "landing") {
    return (
      <div className="contrib-panel-merge" role="status">
        <ClaudeMark className="contrib-claude-working" />
        <span>
          {job.status === "landing" ? (
            "Keeping the merge…"
          ) : (
            <>
              {!brief && "Claude is combining both versions… "}
              <span className="contrib-clock">{clock}</span> so far. Nothing changes until you choose.
            </>
          )}
        </span>
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <div className="contrib-panel-merge is-failed" role="status">
        <TriangleAlert aria-hidden="true" />
        <div className="contrib-panel-merge-body">
          <p className="contrib-panel-merge-title">Claude couldn&rsquo;t combine the two versions.</p>
          <p className="contrib-strip-detail">{job.error || "Nothing changed."}</p>
          <div className="contrib-panel-actions">
            <QuietAction onClick={() => void run("discard")} disabled={!!busy || disabled}>
              Leave it for now
            </QuietAction>
          </div>
          {error && (
            <p className="contrib-error" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className={cn("contrib-panel-merge", "is-ready")}>
      <ClaudeMark />
      <div className="contrib-panel-merge-body">
        {!brief && <p className="contrib-panel-merge-title">Claude combined both versions.</p>}
        {!brief && job.summary && <p className="contrib-summary">{job.summary}</p>}
        {!brief && (job.files?.length ?? 0) > 0 && (
          <ul className="contrib-files" aria-label="Files Claude combined">
            {job.files.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        )}
        <p className={brief ? "contrib-panel-merge-title" : "contrib-strip-detail"}>
          Nothing changes until you keep it.
        </p>
        <div className="contrib-panel-actions">
          <Button onClick={() => void run("keep")} disabled={!!busy || disabled}>
            {busy === "keep" && <Loader2 data-icon="inline-start" className="animate-spin" />}
            Keep the merge
          </Button>
          <QuietAction onClick={() => void run("discard")} disabled={!!busy || disabled}>
            {busy === "discard" ? "Discarding…" : "Discard the merge"}
          </QuietAction>
        </div>
        {error && (
          <p className="contrib-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}

/** "1:05" since the merge job started — the server's `started_at` when it
 *  sends one, else the first time this page saw the job running. Ticks once
 *  a second only while a job is running. */
function useMergeClock(job: MergeJobView): string {
  const firstSeen = useRef<{ id: string; at: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const running = job.status === "running";
  if (firstSeen.current?.id !== job.id) firstSeen.current = { id: job.id, at: Date.now() };
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);
  const from = job.started_at ? job.started_at * 1000 : firstSeen.current!.at;
  return elapsed(Math.max(0, now - from));
}
