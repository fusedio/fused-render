// One past chat (T:18135-18201, inventory 05 §B "Row anatomy").
//
// Icon, then name, then TIME — the time is LAST and always drawn, so the right
// edge of every row says the same thing whether or not it is hovered. A row
// whose turn is STILL GOING says so instead, in the same word and the same
// shimmer the top strip uses once you are inside that chat: "running" already
// says when, and the two side by side spend the row's last inch saying one
// thing twice.
import type { SessionRow } from "../protocol/types";
import { ago, paneChatUrl, rowPane, sessionTitle } from "./list-rows";

/** The one mark a row wears at rest to say "this chat was about a file"
 *  (T:18122-18131, verbatim). */
function FileIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

export interface RecentRowProps {
  session: SessionRow;
  file: string | null;
  /** Same file: the session becomes current IN PLACE — a param write, a
   *  history load and a live-run adopt, no reload (T:18189-18197). */
  onOpen(sessionId: string): void;
  /** Another file: a chat about it does not belong in this pane, so the HOST is
   *  sent to that file with the session attached — the same URL "open this
   *  task" makes from the Tasks list (T:18183-18188). */
  onNavigate?(url: string): void;
  /** A mode holds the reader on the chat they are in (`annNavLocked`, PR3). */
  disabled?: boolean;
}

export function RecentRow({
  session,
  file,
  onOpen,
  onNavigate,
  disabled,
}: RecentRowProps) {
  const pane = rowPane(session, file);
  const label = pane ? paneSlashesBase(pane) : "";
  /**
   * A CHAT THAT HAS NEVER RUN (`sched/waiting-chats`). It has no session id, so
   * none of the three doors above open it: it is opened by the ENTRY it is
   * waiting as, through the url the row was built with
   * (`platform/lib/queue.chatUrl`'s `queued` param). Always a navigation, even
   * on this same target — there is no transcript to swap into place, and the
   * pane has to mount knowing its leader.
   */
  const waiting = session.queuedEntry || "";
  const open = () => {
    if (disabled) return;
    if (waiting) {
      if (session.href) onNavigate?.(session.href);
      return;
    }
    if (pane) {
      onNavigate?.(paneChatUrl(pane, session.id));
      return;
    }
    onOpen(session.id);
  };
  return (
    <div
      className={`c-chat-row${session.running ? " is-running" : ""}${
        waiting ? " is-waiting" : ""
      }${pane ? " has-pane" : ""}`}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(ev) => {
        if (ev.key === "Enter") open();
      }}
    >
      <span className="c-dot">⏺</span>
      <span className="c-row-title">{sessionTitle(session)}</span>
      <span className="c-row-right">
        {/* The icon carries the path too: at rest it is the only part of this
            on screen, so pointing at it has to answer the question it raises. */}
        <span
          className="c-row-fileic"
          aria-hidden="true"
          title={pane || undefined}
        >
          <FileIcon />
        </span>
        {/* Basename on the row, whole path on hover: the folder is already the
            one we are looking at, so the leading path repeated down the list
            says nothing (T:18168-18172). */}
        <span className="c-row-file" title={pane || undefined}>
          {label}
        </span>
        {session.running ? <span className="c-row-run">running</span> : null}
        {/* ONE WORD, IN THE STATE'S OWN INK — `waiting` where a live row says
            `running`. It replaces the time for the same reason `running` does:
            the state is the more useful answer to "when", and the two side by
            side spend the row's last inch saying one thing twice. */}
        {waiting ? <span className="c-row-wait">waiting</span> : null}
        {session.running || waiting ? null : (
          <span className="c-row-sub">
            {ago(session.last_used || session.created_at || 0)}
          </span>
        )}
      </span>
    </div>
  );
}

/** The name the row shows for another file: its basename (T:18170). */
function paneSlashesBase(pane: string): string {
  const norm = /^[A-Za-z]:[\\/]/.test(pane) ? pane.replace(/\\/g, "/") : pane;
  return norm.split("/").pop() || norm;
}
