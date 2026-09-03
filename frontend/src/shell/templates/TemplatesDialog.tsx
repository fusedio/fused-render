// Local dialog chassis for the Templates surface: shadcn <Dialog> (base-ui)
// carrying the contract the shared Modal chassis gave these forms —
//   • `busy`  — Esc / backdrop / ✕ do NOT close while an action is running.
//   • `dirty` — the first close attempt is intercepted with an inline
//     "close again to discard" hint; the NEXT attempt closes. Arming clears on
//     the next interaction inside the dialog (typing / clicking), not a clock.
//   • footer actions call `onClose` directly (an explicit Cancel is explicit
//     intent and bypasses the dirty guard, unlike Esc / backdrop / ✕).
// Callers mount it as `{open && <X/>}`, so `open` is always true here and the
// parent unmounts it on onClose. base-ui owns focus trap + Esc + backdrop;
// focus restore on unmount is done by hand because the abrupt unmount can
// beat base-ui's own return-focus.
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { cn } from "@platform/lib/utils";

export function TemplatesDialog({
  title,
  description,
  onClose,
  busy = false,
  dirty = false,
  footer,
  className,
  initialFocus,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  onClose: () => void;
  busy?: boolean;
  dirty?: boolean;
  footer?: ReactNode;
  // Width / layout overrides for the popup (default is a 640px form dialog).
  className?: string;
  initialFocus?: React.RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const [armed, setArmed] = useState(false);
  const restoreRef = useRef<Element | null>(null);

  useEffect(() => {
    restoreRef.current = document.activeElement;
    return () => {
      (restoreRef.current as HTMLElement | null)?.focus?.();
    };
  }, []);

  return (
    <Dialog
      open
      onOpenChange={(open, details) => {
        if (open) return;
        if (busy) {
          details.cancel();
          return;
        }
        if (dirty && !armed) {
          details.cancel();
          setArmed(true);
          return;
        }
        onClose();
      }}
    >
      <DialogContent
        className={cn("sm:max-w-[640px] gap-4", className)}
        initialFocus={initialFocus}
        // Any interaction with the form disarms a pending discard.
        onKeyDownCapture={(e) => {
          if (armed && e.key !== "Escape") setArmed(false);
        }}
        onPointerDownCapture={(e) => {
          if (armed && !(e.target as HTMLElement).closest("[data-slot=dialog-close]")) setArmed(false);
        }}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description != null && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <div className="flex flex-col gap-4 text-sm">{children}</div>
        {(footer != null || armed) && (
          <DialogFooter className="items-center">
            {armed && (
              <span role="status" className="text-xs text-muted-foreground sm:mr-auto">
                Unsaved changes — close again to discard.
              </span>
            )}
            {footer}
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
