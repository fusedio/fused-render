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

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
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
                      onClose();
                      navigateUrl(href);
                    }}
                  />
                }
                nativeButton={false}
              >
                Open app page
              </Button>
              <DialogClose render={<Button variant="ghost" size="icon-sm" />}>
                <X aria-hidden />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
          </div>
          <DialogDescription>Where it stands, what changed, and who took part.</DialogDescription>
        </DialogHeader>
        <div className="contrib-modal-body">
          <CommunityTab dir={dir} slug={slug} contrib={contrib} variant="sheet" />
        </div>
      </DialogContent>
    </Dialog>
  );
}
