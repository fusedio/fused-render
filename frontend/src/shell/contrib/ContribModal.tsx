// The whole contribution loop in one dialog, for people who work from the
// explorer and never open the app page (owner, 2026-10-22): the Source panel
// (where this copy comes from, its state, every button, Activity with
// Undo), the contributors line, and — for the author's own app — the
// Proposals inbox. Opened from the explorer's three-dot menu
// (apps/explorer/EntryActionsMenu.tsx, the "GitHub" row), which is why this
// file is on the boundary script's shell-open-to-apps list: the app layer
// may import this one shell module and nothing else of the loop.
//
// It reads the folder's state itself (`useAppContrib`), so the explorer
// hands it only the folder. Everything it shows is what the app page shows;
// nothing is decided here.
import { X } from "lucide-react";
import { useAppContrib } from "@platform/lib/contrib";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Contributors } from "./Contributors";
import { ProposalsTab } from "./ProposalsTab";
import { SourcePanel } from "./SourcePanel";
import { authorOf, possessive } from "./words";

export function ContribModal({ dir, slug, onClose }: { dir: string; slug: string; onClose: () => void }) {
  const contrib = useAppContrib(dir);
  const data = contrib.data;
  const inboxOffered = !!data?.flags.inbox && !!data.inbox;
  const kind = data?.panel?.kind ?? data?.copy.kind ?? null;
  const who = data ? authorOf(data.copy) : null;
  const what =
    kind === "github" && who
      ? `A copy of ${possessive(who)} app, installed from a GitHub link.`
      : kind === "remix" && who
        ? `Your remix of ${possessive(who)} app.`
        : kind === "own"
          ? "Your app on GitHub."
          : "This app and GitHub.";

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="grid-rows-[auto_minmax(0,1fr)] gap-4 overflow-hidden p-6 sm:max-w-[680px] max-h-[85vh]"
        showCloseButton={false}
      >
        <DialogHeader className="gap-2">
          <div className="flex items-start justify-between gap-2">
            <DialogTitle className="font-semibold">{slug} on GitHub</DialogTitle>
            <DialogClose render={<Button variant="ghost" size="icon-sm" className="-mt-1 -mr-1" />}>
              <X aria-hidden />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
          <DialogDescription>{what}</DialogDescription>
        </DialogHeader>
        <div className="contrib-modal-body">
          {!contrib.settled && !data ? (
            <Skeleton className="h-[120px] w-full rounded-[10px]" />
          ) : !data ? (
            <p className="contrib-modal-off">
              The Community preview is off for this app. Turn on &ldquo;Where apps come from&rdquo; under
              Preferences &rsaquo; Apps and sharing &rsaquo; Community to see where it comes from, send
              changes, and review proposals.
            </p>
          ) : (
            <>
              <SourcePanel dir={dir} slug={slug} contrib={contrib} />
              <Contributors contrib={data} />
              {inboxOffered && (
                <section className="contrib-modal-inbox" aria-label="Proposals">
                  <h3 className="contrib-modal-h">Proposals</h3>
                  <ProposalsTab dir={dir} slug={slug} contrib={contrib} />
                </section>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
