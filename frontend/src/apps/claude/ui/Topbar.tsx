// The chat's header: TWO ROWS, which is what T actually has (T:3920-4083,
// inventory 04 §E/§F).
//
// Row one is the CONTROL strip — the way out at its left end, the menu at its
// right (`#anntools`: `#back` … `#kebab`), and it is the row PR2/PR3 hang the
// screenshot, comment and annotate seats on. Row two is the chat's IDENTITY
// (`#topbar`): the Claude mark, "Claude", the target's name, then the task
// number and the "running" mark at the right end.
//
// It shipped as one row with all six things in it, which is how a 43px strip in
// a narrow pane ends up with the file name and the id fighting for the same
// inch and the ⋮ pressed against the running mark (Akshil, 2026-09-08). Two
// rows give each half its own line and its own right-hand anchor, and the
// identity line can then ellipsise the one thing that is arbitrarily long — the
// name — without moving anything else.
//
// The name is TASK-nnn and not a session hash: a session IS a task, 1:1, and
// TASK-023 is the identifier every other surface in this app prints, so a
// reader can carry it to the Tasks page and quote it to someone (T:12660-12672).
import { useCallback, useRef } from "react";
import "../styles/composer.css";
import { ClaudeMark } from "./ClaudeMark";
import { Kebab, knownTaskId } from "./Kebab";

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
  /** ACCEPTED AND UNUSED. The raw outgoing text of the last turn fed a "What
   *  was sent" item in this menu, which is gone (Akshil, 2026-09-08 — T opens
   *  that panel from a receipt row under the bubble it belongs to, never from a
   *  menu hanging off the whole conversation). The prop stays in the shape so
   *  the host does not have to stop handing it over mid-flight. */
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
}: TopbarProps) {
  // Where focus goes when the delete confirm closes, whichever way it closed
  // (T:13293, 13319-13321). The menu owns that dialog now; this is the seat it
  // returns to.
  const kebabBtn = useRef<HTMLElement | null>(null);

  const onErased = useCallback(() => {
    // The page must not stay on a transcript that no longer exists
    // (T:13348-13366); the menu has already dropped every cache keyed by it.
    onBack();
  }, [onBack]);

  // The number, or the session hash until it lands (T:12698 — the answer to a
  // slow listing is the old label, never a gap).
  const label =
    taskId ||
    knownTaskId(sessionId) ||
    (sessionId ? sessionId.slice(0, 8) : "");

  return (
    <div className="c-header">
      {/* ROW 1 — the controls, and nothing else. */}
      <div className="c-hdr-tools">
        <button
          type="button"
          className="c-back"
          aria-label="Back to chats"
          disabled={backDisabled}
          onClick={onBack}
        >
          ← Chats
        </button>
        <span className="c-hdr-slack" />
        <Kebab
          agentDir={agentDir}
          file={file}
          sessionId={sessionId}
          btnRef={kebabBtn}
          running={running}
          onErased={onErased}
        />
      </div>
      {/* ROW 2 — who this conversation is with, and about what. */}
      <div className="c-topbar">
        <ClaudeMark className="c-spark" />
        <span className="c-tb-title">Claude</span>
        <span className="c-tb-file" title={subtitle || undefined}>
          {subtitle ?? ""}
        </span>
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
      </div>
    </div>
  );
}
