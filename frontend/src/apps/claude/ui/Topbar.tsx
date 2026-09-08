// The chat's identity strip and the way out of it (T:4063-4082 markup,
// T:3942 / T:12972 for Back, inventory 04 §E/§F).
//
// The strip carries the spark, "Claude", the target's name, the TASK number and
// a "running" mark; Back and the kebab bracket it. `#back` lived at the right
// end of the topbar and sits at the left of the control strip so the row reads
// [← Chats … ⋮] with the identity between them.
//
// The name is TASK-nnn and not a session hash: a session IS a task, 1:1, and
// TASK-023 is the identifier every other surface in this app prints, so a
// reader can carry it to the Tasks page and quote it to someone (T:12660-12672).
import { useCallback, useRef, useState } from "react";
import "../styles/composer.css";
import { EraseDialog } from "./EraseDialog";
import { Kebab, forgetTaskCaches, knownTaskId } from "./Kebab";
import { SentPop } from "./SentPop";

export interface TopbarProps {
  agentDir: string | null;
  file: string | null;
  sessionId: string;
  /** The target's own name under "Claude" (`#banner-sub`). */
  subtitle?: string;
  /** TASK-nnn, when the listing has been read (`#session`). */
  taskId?: string;
  /** A turn is live: one source of truth for the mark and the composer's stop
   *  square (T:1342-1349). */
  running: boolean;
  /** Back to chats — clears the transcript and lands on the landing composer
   *  (`newChat`). Refused while an annotation mode holds the reader here
   *  (`annNavLocked`, PR3). */
  onBack(): void;
  backDisabled?: boolean;
  /** The raw outgoing text of the turn the "what was sent" item shows. */
  lastRaw?: string;
}

export function Topbar({
  agentDir,
  file,
  sessionId,
  subtitle,
  taskId,
  running,
  onBack,
  backDisabled,
  lastRaw,
}: TopbarProps) {
  const [erasing, setErasing] = useState(false);
  const [sent, setSent] = useState(false);
  // Where focus goes when the confirm closes, whichever way it closed
  // (T:13293, 13319-13321).
  const kebabBtn = useRef<HTMLElement | null>(null);

  const onErased = useCallback(
    (key: string) => {
      // Every cache keyed by this session is now a lie, and the page must not
      // stay on a transcript that no longer exists (T:13348-13366).
      forgetTaskCaches(key);
      setErasing(false);
      onBack();
    },
    [onBack],
  );

  // The number, or the session hash until it lands (T:12698 — the answer to a
  // slow listing is the old label, never a gap).
  const label = taskId || knownTaskId(sessionId) || (sessionId ? sessionId.slice(0, 8) : "");

  return (
    <>
      <div className="c-topbar">
        <button
          type="button"
          className="c-back"
          aria-label="Back to chats"
          disabled={backDisabled}
          onClick={onBack}
        >
          ← Chats
        </button>
        <span className="c-spark">✻</span>
        <span className="c-tb-title">Claude</span>
        <span className="c-tb-file">{subtitle ?? ""}</span>
        {label ? (
          // The full session id stays reachable for the reader who actually
          // wants it (a log line, a bug report) without spending header width
          // on it (T:12700).
          <span className="c-session" title={sessionId || undefined}>
            {label}
          </span>
        ) : null}
        {/* `aria-live="polite"`, not assertive: this is an ambient state, and a
            reader that interrupts to announce the start of every turn is worse
            than one that mentions it when it next comes up for air (T:4078). */}
        {running ? (
          <span className="c-tb-run" aria-live="polite">
            running
          </span>
        ) : null}
        <Kebab
          agentDir={agentDir}
          file={file}
          sessionId={sessionId}
          btnRef={kebabBtn}
          running={running}
          onErase={() => setErasing(true)}
          onWhatWasSent={lastRaw ? () => setSent(true) : undefined}
        />
      </div>
      <EraseDialog
        open={erasing}
        sessionKey={sessionId}
        taskId={label || undefined}
        returnFocusTo={kebabBtn}
        onClose={() => setErasing(false)}
        onErased={onErased}
      />
      <SentPop
        open={sent}
        onClose={() => setSent(false)}
        outgoing={lastRaw ?? ""}
      />
    </>
  );
}
