// The delete confirmation, and nothing else.
//
// It is the only surface on this page that DESTROYS something, and it is the
// reason the flow has a `Pending` state at all: no click goes straight to a
// delete, every one becomes a target the user reads back first. It states the
// bytes it frees, because that number is the whole reason someone is here.
import { type Pending } from "./LocalTab";
import { type AiModelDeleteTarget } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Identifier } from "@platform/ui/flow/Typography";

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
  /** `label` is the past-tense phrase the toast reports — built HERE, where
   *  the target's name is already in hand. */
  onConfirm: (targets: AiModelDeleteTarget[], label: string) => void;
}) {
  const repo = pending?.kind === "repo" ? pending.repo : null;
  return (
    <Dialog
      open={repo !== null}
      onOpenChange={(open) => {
        // A delete in flight is not dismissable — the listing on screen is
        // about to change and the dialog is what says so.
        if (!open && !busy) onClose();
      }}
    >
      {repo && (
        <DialogContent showCloseButton={!busy} aria-busy={busy || undefined}>
          <DialogHeader>
            <DialogTitle>Delete {repo.id}?</DialogTitle>
            <DialogDescription>
              Removes every revision of <b className="text-foreground">{repo.id}</b> from this machine and
              frees <b className="text-foreground">{formatSize(repo.size)}</b>. Anything that needs it again
              downloads it again.
            </DialogDescription>
          </DialogHeader>
          <Identifier className="break-all">{repo.path}</Identifier>
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => onConfirm([{ dir: repo.dir }], `deleted ${repo.id}`)}
            >
              {busy ? "Deleting…" : `Delete · ${formatSize(repo.size)}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}
