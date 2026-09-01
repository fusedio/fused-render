// The delete confirmation, and nothing else.
//
// It is the only surface on this page that DESTROYS something, and it is the
// reason the flow has a `Pending` state at all: no click goes straight to a
// delete, every one becomes a target the user reads back first. It states the
// bytes it frees, because that number is the whole reason someone is here.
//
// There was a SECOND dialog here, for deleting one revision of a repo, and it
// went with the revision UI on 2026-08-24 (see `RepoCard`'s headstone). It was
// the most carefully written dialog on the page — it stated the blobs a
// revision shares with its siblings and therefore does NOT free — and none of
// that was a question anyone brought to this page: it asked a reader to pick a
// git commit off a folder whose only real question is what it costs. The whole
// repo is the target now, which is the number the card shows.
//
// Split out of the page file so the Local tab reads as its grid plus a dialog
// prop, rather than as a grid with 90 lines of modal markup hanging off the
// bottom of the same component.
import { type Pending } from "./LocalTab";
import { type AiModelDeleteTarget } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { Modal } from "@platform/ui/modal/Modal";
import { Button } from "@platform/shadcn/ui/button";
import { Spinner } from "@platform/shadcn/ui/spinner";

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
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={() => onConfirm([{ dir: pending.repo.dir }], `deleted ${pending.repo.id}`)}
            >
              {busy && <Spinner data-icon="inline-start" />}
              {busy ? "Deleting…" : `Delete · ${formatSize(pending.repo.size)}`}
            </Button>
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

    </>
  );
}
