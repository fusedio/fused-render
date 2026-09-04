// The attached picture, full size, over everything. One image and one ✕ — the
// ✕ on the composer's chip is what REMOVES the picture; this only shows it.
//
// On shadcn's `Dialog` for the two dismissals a hand-rolled overlay has to
// re-implement: Escape and a click outside. The BACKDROP and the POPUP are base
// UI's own parts rather than `DialogOverlay` / `DialogContent`: the overlay
// ships a 10% black wash, a backdrop blur and a fade this scrim does not have,
// and the content ships a small centered card with a close button of its own —
// six cancellations for a box whose whole spec is "cover the screen".
//
// `modal={false}` on purpose: the modal default adds a focus trap AND a scroll
// lock, and the scroll lock pads the document to compensate for the scrollbar
// — a layout shift behind a picture that is only being LOOKED at. The popup
// covers the whole viewport, so its own `onClick` is what closes on a click
// outside the image; callers keep the `stopPropagation` on the image itself,
// and their existing window keydown listeners are harmless beside the Dialog's
// own Escape.
import type { ComponentProps, ReactNode } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";

import { cn } from "@platform/lib/utils";
import { Dialog, DialogPortal } from "@platform/shadcn/ui/dialog";

import { INHERIT_FONT_FACE } from "./classes";

export function Lightbox({
  open,
  onClose,
  label,
  className,
  children,
  ...props
}: Omit<ComponentProps<"div">, "children"> & {
  open: boolean;
  /** Called on Escape, and on a click that lands anywhere but the content. */
  onClose: () => void;
  /** Names the overlay for a screen reader — "The attached picture", etc. */
  label: string;
  children: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      modal={false}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogPortal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-[60] bg-[var(--scrim-veil)]" />
        <DialogPrimitive.Popup
          aria-label={label}
          className={cn(
            "fixed inset-0 z-[60] flex cursor-zoom-out items-center justify-center bg-transparent p-10 outline-none",
            className,
          )}
          onClick={onClose}
          {...props}
        >
          {children}
        </DialogPrimitive.Popup>
      </DialogPortal>
    </Dialog>
  );
}

/** The picture is the content: the cursor stops meaning "click to close" over
 *  it, and the click is swallowed there too (by the caller's own
 *  `stopPropagation`). */
export const lightboxImageClass =
  "max-h-full max-w-full cursor-default rounded-[8px] shadow-[0_18px_48px_var(--scrim-lift)]";

/** Fixed scrim colours, not theme ones: this chip sits on arbitrary photo
 *  pixels, so the values are the same in both palettes by design — and they are
 *  tokens all the same. */
export function LightboxClose({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "absolute top-4 right-5 h-[34px] w-[34px] cursor-pointer rounded-[8px] border-none bg-[var(--scrim-chip-bg)] text-[15px] leading-none text-[var(--scrim-fg)]",
        INHERIT_FONT_FACE,
        "hover:bg-[var(--scrim-chip-bg-hover)]",
        className,
      )}
      {...props}
    />
  );
}

/** The webcam overlay's inner column: viewfinder, then Capture. Swallows the
 *  backdrop's click-to-close (and its zoom-out cursor) the way the lightbox's
 *  image does. */
export function LightboxBox({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex max-h-full min-h-0 max-w-full cursor-default flex-col items-center gap-[14px]",
        className,
      )}
      {...props}
    />
  );
}

/** The live view, mirrored the way every video-call self-view is: an unmirrored
 *  preview makes aiming the camera feel inverted. Only the PREVIEW is flipped —
 *  the capture draws the raw frame, so what lands on disk is what the lens
 *  saw. */
export const webcamVideoClass =
  "max-h-[calc(100vh_-_180px)] max-w-full rounded-[8px] bg-[var(--scrim-letterbox)] shadow-[0_18px_48px_var(--scrim-lift)] [transform:scaleX(-1)]";
