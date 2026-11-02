// Stage B's block list on the app page (CONTRIB_LOOP.md §18.4 B4, §18.7):
//
//   BlockedStrip   above the Source panel's own strip, from `panel.block`
//                  (a Marketplace copy) or `panel.block_warn` (a GitHub-link
//                  copy running a version Fused blocked for the Marketplace).
//                  Says what Fused found, what to do about it, and where the
//                  copy went: a HARD block rolled it back (no Undo — Undo
//                  would run the blocked code again), a SOFT block paused
//                  updates and asks before opening ("Open anyway", the
//                  server's `confirm-block`), a warning changed nothing.
//   BlockedNotice  the whole Overview when opening the app answered 423
//                  (`blocks.gate`: a hard block still stands — every release
//                  blocked, or the roll-back is waiting on Merge with Claude).
//
// The words are built here from the server's facts (reason, what to do, the
// release date it rolled back to); the buttons stay the server's, except
// "Remove this app" (`remove-app`), which opens the shell's own uninstall
// dialog rather than posting anything.
import { useState } from "react";
import { ShieldAlert, ShieldCheck, Trash2 } from "lucide-react";
import { softBlockWaiting, type ContribAction, type CopyBlock } from "@platform/lib/contrib";
import { releaseDate } from "@platform/lib/format";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { Button } from "@platform/shadcn/ui/button";
import { SoftBlockConfirm } from "@platform/ui/SoftBlockConfirm";
import { QuietAction, StatusStrip } from "./StatusStrip";

/** The server's id for "Remove this app" (hard block, no release left). */
export const REMOVE_APP_ID = "remove-app";
export { CONFIRM_BLOCK_ID } from "@platform/ui/SoftBlockConfirm";

/** "Rolled back to the release of Mar 4." — or null when there was none. */
function rolledBack(block: CopyBlock): string | null {
  if (!block.rolled_back_to || block.no_target) return null;
  const when = block.rolled_back_date || releaseDate(block.rolled_back_to_at);
  return when ? `Rolled back to the release of ${when}.` : "Rolled back to the release before it.";
}

function folderName(dir: string): string {
  return dir.replace(/\/+$/, "").split("/").pop() || dir;
}

export function BlockedStrip({
  dir,
  block,
  warn,
  who,
  actions,
  disabled,
  onChanged,
}: {
  dir: string;
  block: CopyBlock | null | undefined;
  warn: CopyBlock | null | undefined;
  /** The author's name, for the GitHub-link warning. */
  who: string;
  /** The panel's buttons — `remove-app` is drawn here when the server sends it. */
  actions: ContribAction[];
  disabled?: boolean;
  /** Re-read the panel (after Open anyway). */
  onChanged: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const name = folderName(dir);

  if (block) {
    const removeOffered = actions.some((a) => a.id === REMOVE_APP_ID);
    if (block.level === "soft") {
      const confirmed = !softBlockWaiting(block);
      return (
        <>
          <StatusStrip
            tone="attention"
            icon={<ShieldAlert />}
            sentence={<>Fused paused updates for this app: {block.reason}</>}
            detail={
              <>
                {block.what_to_do && <p className="contrib-block-todo">{block.what_to_do}</p>}
                {confirmed && <p className="contrib-block-note">You chose to keep using this version.</p>}
              </>
            }
            primary={
              confirmed ? null : (
                <Button variant="outline" onClick={() => setAsking(true)} disabled={disabled}>
                  Open anyway
                </Button>
              )
            }
          />
          <SoftBlockConfirm
            open={asking}
            dir={dir}
            name={name}
            block={block}
            onClose={() => setAsking(false)}
            onConfirmed={onChanged}
          />
        </>
      );
    }
    // Hard: stopped and rolled back, or stopped with nowhere to go.
    const back = rolledBack(block);
    const stuck = !block.rolled_back_to || !!block.no_target;
    return (
      <StatusStrip
        tone="attention"
        icon={<ShieldAlert />}
        sentence={<>Fused blocked the version of this app you were running: {block.reason}</>}
        detail={
          <>
            {back && <p className="contrib-block-note">{back}</p>}
            {block.conflict && (
              <p className="contrib-block-note">
                Your changes overlap the roll-back, so the app stays stopped until Merge with Claude finishes it.
              </p>
            )}
            {stuck && !block.conflict && (
              <p className="contrib-block-note">Every release of this app is blocked, so it stays stopped.</p>
            )}
            {block.what_to_do && <p className="contrib-block-todo">{block.what_to_do}</p>}
          </>
        }
        secondary={
          removeOffered || stuck ? (
            <QuietAction tone="danger" onClick={() => openUninstallApp({ path: dir, name })} disabled={disabled}>
              Remove this app
            </QuietAction>
          ) : null
        }
      />
    );
  }

  if (warn) {
    return (
      <StatusStrip
        tone="attention"
        icon={<ShieldAlert />}
        sentence={
          <>
            Fused blocked a version of this app that you&rsquo;re running: {warn.reason} You follow {who} directly, so
            nothing was changed.
          </>
        }
        detail={warn.what_to_do ? <p className="contrib-block-todo">{warn.what_to_do}</p> : null}
      />
    );
  }
  return null;
}

/** The app page's whole Overview when opening the app answered 423: a hard
 *  block stands. Reason, what to do, where the copy went, and the one way
 *  out the user always has — removing the app. */
export function BlockedNotice({ dir, block }: { dir: string; block: CopyBlock }) {
  const name = folderName(dir);
  const back = rolledBack(block);
  return (
    <section className="contrib-blocked-notice" role="alert">
      <div className="contrib-blocked-mark" aria-hidden="true">
        <ShieldAlert />
      </div>
      <div className="contrib-blocked-body">
        <h2 className="contrib-blocked-title">Fused blocked this app</h2>
        <p className="contrib-blocked-reason">{block.reason}</p>
        {block.what_to_do && (
          <p className="contrib-blocked-todo">
            <ShieldCheck aria-hidden="true" />
            <span>{block.what_to_do}</span>
          </p>
        )}
        <p className="contrib-blocked-where">
          {back ??
            (block.conflict
              ? "Your changes overlap the roll-back. The app stays stopped until Merge with Claude finishes it."
              : "Every release of this app is blocked, so it stays stopped.")}
        </p>
        <div className="contrib-blocked-actions">
          <Button variant="destructive" onClick={() => openUninstallApp({ path: dir, name })}>
            <Trash2 data-icon="inline-start" />
            Remove this app
          </Button>
        </div>
      </div>
    </section>
  );
}
