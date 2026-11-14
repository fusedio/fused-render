// The Canvases page's two words on the reference canvases (mirror-status.ts
// decides what they are): a quiet chip in the page head — how many there are
// and when they were last updated, a press updates them now — and one notice
// for canvases the move to the new folder layout left behind. There is no
// browser for the copies themselves; they are for Claude to search.
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight, CircleAlert, Loader2, TriangleAlert } from "lucide-react";
import {
  getMirrorStatus,
  runMirror,
  type CanvasesStatus,
  type LayoutPending,
  type MirrorSummary,
  type WorkbenchEnv,
} from "./api";
import {
  layoutNotice,
  mirrorChip,
  mirrorPollDelay,
  mirrorReadIsCurrent,
  mirrorRunRefusal,
  type MirrorChip as Chip,
  type MirrorReadTicket,
} from "./mirror-status";

type EnvStatus = CanvasesStatus["envs"][WorkbenchEnv];

interface Live {
  /** The status answer this was read after: a newer one replaces it. */
  seed: EnvStatus | null;
  mirror: MirrorSummary | null;
  layout: LayoutPending | null;
}

/** `env`'s mirror status and pending layout: what the page's own status read
 *  carried, kept current from the mirror's status route — closely while a run
 *  is in flight, slowly otherwise, and not at all for an environment where no
 *  run would start (mirrorPollDelay). `run` asks for an update now; a refusal
 *  worth telling goes to `onError`. */
export function useReferenceMirror(
  env: WorkbenchEnv | null,
  status: CanvasesStatus | null,
  onError: (message: string) => void,
) {
  const seed = (env && status?.envs?.[env]) || null;
  const [live, setLive] = useState<Live | null>(null);
  const [starting, setStarting] = useState(false);
  // A read made after this seed stands until the page reads its status again.
  const current = live && live.seed === seed ? live : null;
  const mirror = current ? current.mirror : (seed?.mirror ?? null);
  const layout = current ? current.layout : (seed?.layout ?? null);

  const envRef = useRef(env);
  envRef.current = env;
  const seedRef = useRef(seed);
  seedRef.current = seed;
  // Bumped by a refresh the user asks for: a status read that set out before
  // it (or before a newer page status) is dropped when it lands.
  const genRef = useRef(0);

  const delay = mirrorPollDelay(mirror);
  useEffect(() => {
    if (!env || delay === null) return;
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      // A hidden tab reads nothing; it catches up on its next tick.
      if (!document.hidden) {
        const sent: MirrorReadTicket<EnvStatus | null> = {
          gen: genRef.current,
          seed: seedRef.current,
        };
        try {
          const next = await getMirrorStatus(env);
          if (cancelled) return;
          if (mirrorReadIsCurrent(sent, genRef.current, seedRef.current)) {
            setLive({ seed: sent.seed, mirror: next, layout: next.layout });
          }
        } catch {
          // The chip keeps what it last knew; the next tick asks again.
        }
      }
      if (!cancelled) timer = window.setTimeout(() => void tick(), delay);
    };
    timer = window.setTimeout(() => void tick(), delay);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [env, delay]);

  const run = useCallback(async () => {
    if (!env) return;
    setStarting(true);
    // A poll already out read the state from before this run: dropped.
    genRef.current += 1;
    const sentSeed = seedRef.current;
    try {
      const answer = await runMirror(env);
      // Another environment was picked while this was out.
      if (envRef.current !== env) return;
      // So is one sent while this was out: it may have read the server
      // before the run began, and this answer is the newer word.
      genRef.current += 1;
      setLive({ seed: sentSeed, mirror: answer.status, layout: answer.status.layout });
      const refusal = answer.started ? null : mirrorRunRefusal(answer.reason);
      if (refusal) onError(refusal);
    } catch (e) {
      if (envRef.current !== env) return;
      onError(`Reference canvases could not be updated: ${(e as Error).message}`);
    } finally {
      setStarting(false);
    }
  }, [env, onError]);

  return { mirror, layout, run, starting };
}

function ChipIcon({ tone }: { tone: Chip["tone"] }) {
  if (tone === "running") {
    return <Loader2 className="canvases-mirror-icon is-spinning" aria-hidden />;
  }
  if (tone === "warning") return <TriangleAlert className="canvases-mirror-icon" aria-hidden />;
  if (tone === "error") return <CircleAlert className="canvases-mirror-icon" aria-hidden />;
  return null;
}

/** The page head's reference-canvases chip. Renders nothing for a reader who
 *  is not using them. */
export function MirrorChip({
  mirror,
  starting,
  onRun,
}: {
  mirror: MirrorSummary | null;
  /** A run was just asked for and the answer is not back yet. */
  starting: boolean;
  onRun: () => void;
}) {
  const chip = mirrorChip(mirror);
  if (!chip) return null;
  const className = `canvases-mirror is-${chip.tone}`;
  const body = (
    <>
      <ChipIcon tone={chip.tone} />
      <span className="canvases-mirror-text">{chip.text}</span>
    </>
  );
  if (!chip.canRun) {
    return (
      <span className={className} role="status" title={chip.title}>
        {body}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={className}
      title={chip.title}
      aria-label={`${chip.text}. ${chip.title}`}
      disabled={starting}
      onClick={onRun}
    >
      {body}
    </button>
  );
}

/** Canvases still at their old path because something stands at the new one.
 *  One line; the names and reasons open under it. */
export function LayoutNotice({ layout }: { layout: LayoutPending | null }) {
  const notice = layoutNotice(layout);
  if (!notice) return null;
  return (
    <details className="canvases-layout-notice">
      <summary>
        <TriangleAlert className="canvases-layout-notice-icon" aria-hidden />
        {notice.summary}
        <ChevronRight className="canvases-layout-notice-chevron" aria-hidden />
      </summary>
      <div className="canvases-layout-notice-body">
        <p>
          Each one is still in its old folder, and moves once what is in its way is gone.
        </p>
        {notice.detail && <p>{notice.detail}</p>}
        {notice.conflicts.length > 0 && (
          <ul>
            {notice.conflicts.map((c) => (
              <li key={c.name} title={c.why}>
                <code>{c.name}</code>: {c.why}
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}
