// The Community sheet (CONTRIB_LOOP.md §19.5): the app page's Community tab
// as a dialog, for people who work from the explorer and never open the app
// page. Opened from the explorer's three-dot menu
// (apps/explorer/EntryActionsMenu.tsx, the "Community" row), which is why
// this file is on the boundary script's shell-open-to-apps list: the app
// layer may import this one shell module and nothing else of the loop.
//
// It reads the folder's state itself (`useAppContrib`, and the ladder inside
// CommunityTab), so the explorer hands it only the folder. The body IS the
// tab (`CommunityTab variant="sheet"`): same content, same server reads,
// nothing decided here. "Open app page" goes to the tab itself.
//
// LOCKED WHILE AN ACTION RUNS (as ShareAppModal is while it works): the
// tab's own progress line and outcome sentence live inside the sheet, so
// closing it mid-Publish or mid-Accept dropped both — the job ran on, and a
// failure read as nothing happening. Escape, an outside click, the close
// button and a plain click on "Open app page" all wait until it ends (a new
// tab from that link leaves this sheet where it is).
import { useState } from "react";
import { X } from "lucide-react";
import { useAppContrib } from "@platform/lib/contrib";
import { navigateUrl } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { appPageUrl } from "../current-apps-lib";
import { CommunityTab } from "./CommunityTab";

export function ContribModal({ dir, slug, onClose }: { dir: string; slug: string; onClose: () => void }) {
  const contrib = useAppContrib(dir);
  const href = appPageUrl(dir, "community");
  const [busy, setBusy] = useState(false);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent
        className="grid-rows-[auto_minmax(0,1fr)] gap-4 overflow-hidden p-6 sm:max-w-[880px] max-h-[85vh]"
        showCloseButton={false}
      >
        <DialogHeader className="gap-2">
          <div className="flex items-start justify-between gap-2">
            <DialogTitle className="font-semibold">{slug} · Community</DialogTitle>
            <div className="-mt-1 -mr-1 flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                render={
                  <a
                    href={href}
                    className="contrib-modal-open"
                    onClick={(e) => {
                      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                      e.preventDefault();
                      if (busy) return;
                      onClose();
                      navigateUrl(href);
                    }}
                  />
                }
                nativeButton={false}
              >
                Open app page
              </Button>
              <DialogClose render={<Button variant="ghost" size="icon-sm" disabled={busy} />}>
                <X aria-hidden />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
          </div>
          <DialogDescription>Where it stands, what changed, and who took part.</DialogDescription>
        </DialogHeader>
        <div className="contrib-modal-body">
          <CommunityTab dir={dir} slug={slug} contrib={contrib} variant="sheet" onBusy={setBusy} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
