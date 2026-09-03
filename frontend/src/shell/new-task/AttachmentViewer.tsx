// The viewer (the claude template's #shotview, ported): a chip proves a thing
// EXISTS, it cannot show what is in it, so clicking one opens this. A PICTURE
// opens fitted and a second click swaps to natural size with the box scrolling;
// a FILE opens in its own fused-render template, sealed (D616) — the only answer
// to "is this the right file" that a name cannot give.
//
// A nested shadcn Dialog over the card: modal on purpose — the one thing the
// user is doing here is looking at one attachment. Escape and the scrim close
// it, and only it: the Escape is captured at the document before the card's own
// dialog can see it.
//
// The frame is UNMOUNTED on every exit, and that is load-bearing rather than a
// tidy-up: a template is a RUNNING document (a warm python worker behind it, a
// poll, possibly a map redrawing), so one left mounted behind a shut viewer
// goes on costing all of it.
import { useEffect } from "react";
import { FileTextIcon } from "lucide-react";
import { rawUrl } from "@platform/lib/api";
import { THUMB_SEAL } from "@platform/lib/frame-focus";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@platform/shadcn/ui/dialog";
import { cn } from "@platform/lib/utils";
import type { TaskImage } from "./attachments";

export function AttachmentViewer({
  viewer,
  zoom,
  onToggleZoom,
  previewSrc,
  previewWait,
  frameLoaded,
  onFrameLoad,
  onClose,
}: {
  viewer: TaskImage;
  zoom: boolean;
  onToggleZoom: () => void;
  // A file's preview: the src once the stat has answered, null for every "no
  // preview" case. `frameLoaded` because the promise the caption makes is
  // about the PAGE, not the URL.
  previewSrc: string | null;
  previewWait: boolean;
  frameLoaded: boolean;
  onFrameLoad: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, { capture: true });
    return () => document.removeEventListener("keydown", onKey, { capture: true });
  }, [onClose]);

  const isImage = viewer.kind === "image";
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        showCloseButton={false}
        className="block gap-0 rounded-lg p-0 sm:max-w-4xl"
        aria-label={isImage ? "Attached image" : "Attached file"}
      >
        <DialogTitle className="sr-only">
          {isImage ? "Attached image" : "Attached file"}
        </DialogTitle>
        <div className="relative flex max-h-[70vh] items-center justify-center overflow-auto bg-muted/30">
          {isImage ? (
            <img
              className={cn(
                zoom ? "max-w-none cursor-zoom-out" : "max-h-[70vh] max-w-full cursor-zoom-in object-contain",
              )}
              src={viewer.thumb ?? rawUrl(viewer.path)}
              alt="attached image"
              onClick={onToggleZoom}
            />
          ) : (
            <>
              {previewSrc && (
                <iframe
                  className="h-[70vh] w-full border-0 bg-background"
                  {...THUMB_SEAL}
                  src={previewSrc}
                  title=""
                  tabIndex={-1}
                  aria-hidden="true"
                  onLoad={onFrameLoad}
                />
              )}
              {(previewWait || (!!previewSrc && !frameLoaded)) && (
                <p className="absolute inset-x-0 top-1/2 -translate-y-1/2 text-center text-xs text-muted-foreground">
                  loading preview…
                </p>
              )}
              {!previewWait && !previewSrc && (
                <p className="py-16 text-xs text-muted-foreground">No preview for this file.</p>
              )}
            </>
          )}
        </div>
        <div className="flex items-center gap-3 border-t border-border px-3 py-2 text-xs">
          {!isImage && (
            <span className="flex items-center gap-1.5 font-medium">
              <FileTextIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
              {viewer.name}
            </span>
          )}
          <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground">
            {viewer.path || "uploading…"}
          </span>
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
