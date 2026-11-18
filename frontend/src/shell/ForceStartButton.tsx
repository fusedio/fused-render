// FORCE START, ON A CARD THAT SHOWS A QUEUED TASK WITH NO CHAT TO SHOW IT IN.
//
// A queued task that already has a conversation gets the verb from the chat
// itself (`apps/claude/ui/Waiting.tsx`, the card over the composer), and a
// Tasks row gets it from its hover strip (`ScheduleTaskViews`). The rail's
// cards had a third case and no button for it: a task that is queued BEFORE it
// has a session — a new task waiting for its folder — draws a placeholder where
// the chat will be ("Starting the task…", the Queued pill, "no conversation
// yet"), and a placeholder had nothing to press. The reader was looking at a
// task they could see was waiting, with no way to stop it waiting short of
// leaving the card for the Tasks page.
//
// This is that button, once, for every such placeholder. The same gate, words
// and call as the other two seats (`canForceStart`, `FORCE_START_LABEL`,
// `forceStart`), so the three can never disagree about when the verb is offered
// or what it is called.
import { useRef, useState } from "react";
import { forceStart } from "@platform/lib/api";
import {
  FORCE_START_HINT, FORCE_START_LABEL, QUEUED_WORD, canForceStart, pendingEntryId, queueCaption, type QueueFacts,
} from "@platform/lib/queue";
import { pokeTasks } from "./tasksPulse";

/** The sentence a placeholder says for a queued task, in the Tasks page's own
 *  words: "Queued · after TASK-038 | 2nd", or just "Queued" when the server
 *  could place it nowhere. "" for a task that is not queued. */
export function queuedLine(facts: QueueFacts | null | undefined): string {
  const caption = facts ? queueCaption(facts) : null;
  if (!caption) return "";
  return caption.text && caption.text !== QUEUED_WORD ? `Queued · ${caption.text}` : "Queued";
}

export interface ForceStartButtonProps {
  /** The task's row, or whatever of it is known. Nothing is drawn until it says
   *  the task stands in a folder's line (`canForceStart`): a card that has not
   *  heard yet offers nothing rather than a press the server would refuse. */
  facts: QueueFacts | null | undefined;
  /** Who to start: an entry id, or the row's key. A `pending:<entry>` key is
   *  sent as its entry id (see `press`). */
  target: { entry_id?: string; key?: string };
  /** After the server answered, either way — the card's own "look again". */
  onDone?: () => void;
}

export function ForceStartButton({ facts, target, onDone }: ForceStartButtonProps) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  // The guard is a ref and not the state above: two presses inside one render
  // both read `busy` as false, and the second would start a second run.
  const asking = useRef(false);
  if (!facts || !canForceStart(facts)) return null;
  const press = async () => {
    if (asking.current) return;
    asking.current = true;
    setBusy(true);
    setNote("");
    try {
      // `started: false` is an honest answer (the task went on its own a moment
      // ago): nothing to say, the refetch below shows where it is now.
      // BY ENTRY ID WHENEVER THERE IS ONE. A queued task with no session is
      // filed under `pending:<entry>` and its row is REKEYED the moment its run
      // mints a session — a key read off a row painted a second ago can name
      // nothing by the time the press lands. The entry id inside it does not
      // move (`api_queue_force`'s own note).
      const entry = target.entry_id || pendingEntryId(target.key);
      await forceStart(entry ? { entry_id: entry } : target);
    } catch (e) {
      setNote(e instanceof Error && e.message ? e.message : "The queue did not take that.");
    } finally {
      asking.current = false;
      setBusy(false);
      // Every reader of the rows (the rail, the Tasks page, the sidebar) looks
      // again now, rather than at its own next tick.
      pokeTasks();
      onDone?.();
    }
  };
  return (
    <div className="force-start">
      <button
        type="button"
        className="force-start-btn"
        title={FORCE_START_HINT}
        disabled={busy}
        onClick={(e) => {
          // A card's body is a door in places (a press opens the task): this
          // press is the button's alone.
          e.stopPropagation();
          void press();
        }}
      >
        {FORCE_START_LABEL}
      </button>
      {note ? (
        <span className="force-start-note" role="alert">
          {note}
        </span>
      ) : null}
    </div>
  );
}
