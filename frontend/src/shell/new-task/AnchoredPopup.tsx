// A popover anchored to a TEXT FIELD that keeps typing: the folder combobox and
// the time field both drop a list under an <input> the user goes on editing,
// so focus must stay in the input while the list is up. The shadcn
// PopoverContent has no anchor prop and its Trigger toggles on click, which
// fights "open on focus, stay open while typing" — so this composes the base-ui
// primitives directly with `initialFocus={false}`, wearing the same popup
// classes. Portaled, so the dialog's scrolling body cannot clip it, and
// positioned by base-ui, which flips it upward when the viewport below is short
// (the job the old `popStyle` arithmetic did by hand).
import type { ReactNode, RefObject } from "react";
import { Popover as PopoverPrimitive } from "@base-ui/react/popover";
import { cn } from "@platform/lib/utils";

export function AnchoredPopup({
  open,
  onClose,
  anchor,
  children,
  className,
  matchWidth = false,
}: {
  open: boolean;
  // Asked to close by base-ui (outside press, focus leaving). A press on the
  // anchor itself is NOT an outside press here: the input is the control that
  // owns this list, and clicking it must not blink the list shut and open.
  onClose: () => void;
  anchor: RefObject<HTMLElement | null>;
  children: ReactNode;
  className?: string;
  // A list as wide as the field that opened it.
  matchWidth?: boolean;
}) {
  return (
    <PopoverPrimitive.Root
      open={open}
      onOpenChange={(next, details) => {
        if (next) return;
        if (details.reason === "outside-press") {
          const target = details.event.target as Node | null;
          if (target && anchor.current?.contains(target)) return;
        }
        onClose();
      }}
    >
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Positioner
          anchor={anchor}
          side="bottom"
          align="start"
          sideOffset={4}
          className="isolate z-50"
        >
          <PopoverPrimitive.Popup
            initialFocus={false}
            finalFocus={false}
            data-slot="popover-content"
            className={cn(
              "z-50 flex origin-(--transform-origin) flex-col rounded-lg bg-popover p-1 text-sm text-popover-foreground shadow-sm ring-1 ring-foreground/10 outline-hidden duration-100 motion-safe:data-open:animate-in motion-safe:data-open:fade-in-0 motion-safe:data-closed:animate-out motion-safe:data-closed:fade-out-0",
              matchWidth ? "w-(--anchor-width) min-w-36" : "w-auto",
              className,
            )}
            // Keep focus ON the anchor while a row is clicked: Safari never
            // focuses <button> on click, so a blur-driven close would unmount
            // the row before its click fired.
            onMouseDown={(e) => e.preventDefault()}
          >
            {children}
          </PopoverPrimitive.Popup>
        </PopoverPrimitive.Positioner>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}
