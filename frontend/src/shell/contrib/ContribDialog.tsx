// The one dialog skin the contribution loop's sheets share (Propose, Publish
// as remix, Decline): the Share sheet's own layout (platform/ui/ShareAppModal)
// — a title bar with its close, a padded body, a muted footer — at the Share
// sheet's width, so every app-page sheet is the same size.
//
// `locked` is the in-flight guard: while a send or a publish runs, the sheet
// refuses to close (Escape, outside press and the X alike), so the result has
// somewhere to land and the user never wonders whether closing cancelled it.
import type { ReactNode } from "react";
import { XIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";

export function ContribDialog({
  open,
  onClose,
  locked,
  title,
  description,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  locked?: boolean;
  title: ReactNode;
  /** One sentence under the title saying what the sheet does — always
   *  shown, never sr-only: the person deciding should read it. */
  description: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !locked) onClose();
      }}
    >
      <DialogContent className="gap-0 overflow-hidden p-0 sm:max-w-[560px]" showCloseButton={false}>
        <DialogHeader className="gap-1 px-6 pt-4 pb-3">
          <div className="flex items-center justify-between gap-3">
            <DialogTitle className="truncate text-[15px] font-semibold leading-6">{title}</DialogTitle>
            <DialogClose
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="-mr-2 bg-transparent text-muted-foreground hover:text-foreground"
                />
              }
              disabled={locked}
            >
              <XIcon />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
          <DialogDescription className="m-0 text-[13px] leading-5">{description}</DialogDescription>
        </DialogHeader>
        <div className="contrib-dialog-body flex flex-col gap-4 px-6 pt-1 pb-5">{children}</div>
        {footer && <DialogFooter className="mx-0 mb-0 px-6 py-3">{footer}</DialogFooter>}
      </DialogContent>
    </Dialog>
  );
}
