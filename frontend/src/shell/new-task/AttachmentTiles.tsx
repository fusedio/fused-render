// The attachments, minimal on purpose (Akshil, 2026-08-26: "just the image and
// the x icon on it"): square tiles — a bare thumbnail that OPENS the viewer,
// with a ghost ✕ riding its corner. A NON-PICTURE gets the same footprint with
// the doc glyph and a short filename in place of the picture (thumbnail XOR
// glyph, D613). NO ＋ picker (D618): the entry points are paste on either field
// and drop anywhere on the card.
import { FileTextIcon, XIcon } from "lucide-react";
import { rawUrl } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import type { TaskImage } from "./attachments";

export function AttachmentTiles({
  images,
  onOpen,
  onRemove,
}: {
  images: TaskImage[];
  onOpen: (img: TaskImage) => void;
  onRemove: (img: TaskImage) => void;
}) {
  if (!images.length) return null;
  return (
    <div className="flex flex-wrap gap-2 pt-1">
      {images.map((img) => (
        <div
          key={img.key}
          className={cn(
            "group/tile relative size-16 shrink-0",
            // Uploading: dimmed and breathing until the path lands.
            !img.path && "opacity-60 motion-safe:animate-pulse",
          )}
        >
          <button
            type="button"
            className="flex size-full items-center justify-center overflow-hidden rounded-md border border-border bg-muted/40 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            aria-label={img.kind === "image" ? "View image" : "Preview " + img.name}
            onClick={() => onOpen(img)}
          >
            {img.kind === "image" && (img.thumb || img.path) ? (
              <img src={img.thumb ?? rawUrl(img.path)} alt="" className="size-full object-cover" />
            ) : (
              <span className="flex min-w-0 flex-col items-center gap-1 px-1 text-muted-foreground">
                <FileTextIcon className="size-4" aria-hidden="true" />
                <span className="w-14 truncate text-center text-[10px] leading-tight">{img.name}</span>
              </span>
            )}
          </button>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="absolute top-0.5 right-0.5 size-5 bg-background/80 opacity-0 shadow-xs backdrop-blur-sm transition-opacity group-hover/tile:opacity-100 focus-visible:opacity-100 motion-reduce:transition-none"
            aria-label="Remove attachment"
            onClick={() => onRemove(img)}
          >
            <XIcon />
          </Button>
        </div>
      ))}
    </div>
  );
}
