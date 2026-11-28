// THE PANEL'S CHAT — a task's chat, or with no session the folder's Claude
// home (its composer and Recent chats): ONE ChatMount in the posture a page's
// Claude pane mounts it in (SPEC §56 WS-18). Its params are the host URL's
// `_right` (panel-params.ts), so Back, reload and a copied URL keep it.
//
// The chat does not follow an outside change of `session_id` live — a page
// remounts its pane for that — so the host keys this on the file and on a
// counter it bumps when the query changed from OUTSIDE (a ring's click, a
// Back), never on the chat's own writes.
import { useEffect, useState } from "react";
import { ChatMount } from "@apps/claude";
import { breakoutOpener } from "@platform/lib/breakouts";
import { announcePaneSession } from "@platform/lib/peek-session";
import { useTasksPulseRows } from "@shell/tasksPulse";
import { useHoldOpenTask } from "@shell/task-open-read";
import { createPanelParamsStore, type PanelParamsHost } from "../panel-params";
import { chatHoldKey } from "../workspace-lib";

export interface ChatViewProps {
  file: string;
  /** The view's session, while it names one (read off the view's query). */
  sessionId: string | null;
  /** A chat that has not run yet (`queued=<entry>`), else null. */
  queued: string | null;
  /** A staged ask for this mount only (the host's `R.ask`), else null. */
  initialAsk: string | null;
  host: PanelParamsHost;
  /** This mount's token (panel-params `chatMount`): its writes land only
   *  while it is the chat the host shows. */
  mount: string;
  events: EventTarget;
  onHide: () => void;
  onNavigate: (url: string) => void;
}

export function ChatView({ file, sessionId, queued, initialAsk, host, mount, events, onHide, onNavigate }: ChatViewProps) {
  // One store per mount (the host's key remounts this): built in a lazy
  // initializer so a discarded render binds nothing (params/store's header).
  const [store] = useState(() => createPanelParamsStore(host, events, mount));
  useEffect(() => {
    store.attach();
    return () => store.dispose();
  }, [store]);
  // The session on screen counts as open in a pane (peek-session), as a
  // page's pane says it through `AnnounceUrlSession`.
  useEffect(() => (sessionId ? announcePaneSession(sessionId) : undefined), [sessionId]);
  // …and its task counts as OPEN (WS-33): a reply landing here is read as it
  // arrives and raises no "finished" notice. This mounts only while the chat
  // shows; the reader's window being hidden is task-open-read's own check.
  const rows = useTasksPulseRows(true);
  useHoldOpenTask(chatHoldKey(sessionId, queued, rows));
  return (
    <div className="ws-panel-chat">
      <ChatMount
        file={file}
        chatOnly
        prChip
        recap
        paramsSource="url"
        paramsStore={store}
        onHidePane={onHide}
        // Place tags are links here: no page around this chat to select in.
        pageRoot={null}
        // …and the place itself, not only where in it: no page stands for it.
        placeInHead
        // No ← (owner, 2026-11-28): the × alone, and the rail's + for the
        // folder's chats.
        noBack
        onNavigate={onNavigate}
        onBreakout={breakoutOpener()}
        {...(initialAsk ? { initialAsk } : {})}
      />
    </div>
  );
}
