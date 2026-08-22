// The two delete confirmations, and nothing else.
//
// They are the only surfaces on this page that DESTROY something, and they are
// the reason the flow has a `Pending` state at all: no click goes straight to a
// delete, every one becomes a target the user reads back first. Both state the
// bytes they free, because that number is the whole reason someone is here —
// and the revision dialog additionally states what it does NOT free (blobs
// shared with the surviving revisions), which is the one arithmetic a reader
// cannot do from the card.
//
// Split out of the page file so the Local tab reads as its grid plus a dialog
// prop, rather than as a grid with 90 lines of modal markup hanging off the
// bottom of the same component.
import { type Pending } from "./LocalTab";
import { shortCommit } from "./hub";
import { type AiModelDeleteTarget } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { Modal } from "@platform/ui/modal/Modal";

export function DeleteDialogs({
  pending,
  busy,
  onClose,
  onConfirm,
}: {
  /** What is about to be deleted, or null when nothing is. */
  pending: Pending | null;
  busy: boolean;
  onClose: () => void;
  /** `label` is the past-tense phrase the toast reports — built HERE, where the
   *  target's name is already in hand, rather than reconstructed by the caller
   *  from the target list it was handed back. */
  onConfirm: (targets: AiModelDeleteTarget[], label: string) => void;
}) {
  return (
    <>
    {pending?.kind === "repo" && (
      <Modal
        title={`Delete ${pending.repo.id}?`}
        busy={busy}
        onClose={onClose}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy}
              onClick={() => onConfirm([{ dir: pending.repo.dir }], `deleted ${pending.repo.id}`)}
            >
              {busy ? "Deleting…" : `Delete · ${formatSize(pending.repo.size)}`}
            </button>
          </>
        }
      >
        <p>
          Removes every revision of <b>{pending.repo.id}</b> from this machine and frees{" "}
          <b>{formatSize(pending.repo.size)}</b>. Anything that needs it again downloads it again.
        </p>
        <p className="cc-mono cc-unset">{pending.repo.path}</p>
      </Modal>
    )}

    {pending?.kind === "revision" && (
      <Modal
        title={`Delete revision ${shortCommit(pending.revision.commit)}?`}
        busy={busy}
        onClose={onClose}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy}
              onClick={() =>
                onConfirm(
                  [{ dir: pending.repo.dir, revision: pending.revision.commit }],
                  `deleted ${pending.repo.id} @ ${shortCommit(pending.revision.commit)}`,
                )
              }
            >
              {busy ? "Deleting…" : `Delete · ${formatSize(pending.revision.size)}`}
            </button>
          </>
        }
      >
        <p>
          Removes revision <span className="cc-mono">{shortCommit(pending.revision.commit)}</span>{" "}
          of <b>{pending.repo.id}</b>, freeing <b>{formatSize(pending.revision.size)}</b>.
          {pending.revision.shared > 0 && (
            <>
              {" "}
              The <b>{formatSize(pending.revision.shared)}</b> it shares with the other revisions
              stays.
            </>
          )}
        </p>
        {pending.revision.refs.length > 0 && (
          <p>
            {pending.revision.refs.join(", ")}{" "}
            {pending.revision.refs.length === 1 ? "points" : "point"} at this revision and will be
            removed with it.
          </p>
        )}
        {pending.repo.revisions === 1 && (
          <p>It is the only revision left, so the whole repo folder goes.</p>
        )}
      </Modal>
    )}
    </>
  );
}
