// The card's chassis: a shadcn Dialog (base-ui) carrying the four behaviours
// the shared Modal used to own for this form —
//   • busy: Esc / backdrop / ✕ do NOT close while a save is in flight, and the
//     ✕ is not drawn at all then (a disabled corner button teaches nothing —
//     its title never shows, since a disabled control takes no pointer events);
//   • dirty guard: the first close attempt ARMS (the ✕ turns amber and the
//     footer says why), the next one discards, however long the user takes.
//     Arming is cleared by going back to the form — typing, changing a field,
//     pointing at something — never by a clock (QA 2026-08-18: a 2s timer made
//     the guard unescapable for anyone slower than two seconds). The rules live
//     in platform/ui/modal/dirty-guard.ts and are reused verbatim;
//   • a side column: Browse and Custom recurrence open BESIDE the card, inside
//     the same popup, and the dialog widens to fit. Inside rather than as
//     fixed siblings because a modal base-ui dialog makes everything outside
//     its popup inert — a panel floating next to it would be dead to clicks;
//   • exit animation: the parent renders `{open && <NewJobModal/>}`, so the
//     card holds its own `open` and only calls onClose once base-ui reports the
//     close animation complete.
import {
  useCallback,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
  type SyntheticEvent,
} from "react";
import { XIcon } from "lucide-react";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import {
  decideClose,
  isDisarmingInteraction,
} from "@platform/ui/modal/dirty-guard";

export function TaskDialog({
  title,
  onClose,
  busy = false,
  dirty = false,
  footer,
  children,
  side,
  initialFocus,
  onKeyDown,
}: {
  title: ReactNode;
  onClose: () => void;
  busy?: boolean;
  dirty?: boolean;
  footer?: ReactNode;
  children: ReactNode;
  // The panel beside the card, when one is open.
  side?: ReactNode;
  initialFocus?: RefObject<HTMLElement | null>;
  // The card's own key chord (⌘↩ submits), attached to the body column.
  onKeyDown?: (e: ReactKeyboardEvent<HTMLDivElement>) => void;
}) {
  const [open, setOpen] = useState(true);
  const [armed, setArmed] = useState(false);

  const attemptClose = useCallback(() => {
    const decision = decideClose({ busy, dirty, armed });
    if (decision === "block") return;
    if (decision === "arm") {
      setArmed(true);
      return;
    }
    setOpen(false);
  }, [busy, dirty, armed]);

  // DISARM ON RETURNING TO THE FORM. Presses on the ✕ itself, Escape and Tab
  // are deliberately not disarming (that IS the second press / the way to it).
  const disarm = (key: string | null) => (e: SyntheticEvent) => {
    if (!armed) return;
    const target = e.target as Element | null;
    const inClose = !!target?.closest?.("[data-slot=dialog-close]");
    if (isDisarmingInteraction(key, inClose)) setArmed(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) attemptClose();
      }}
      onOpenChangeComplete={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent
        showCloseButton={false}
        initialFocus={initialFocus}
        className={cn(
          "block gap-0 rounded-lg p-0 transition-[max-width] motion-reduce:transition-none",
          side ? "sm:max-w-5xl" : "sm:max-w-2xl",
        )}
        onPointerDown={disarm(null)}
        onInput={disarm(null)}
        onChange={disarm(null)}
      >
        <div className="flex max-h-[calc(100vh-4rem)] items-stretch">
          <div
            className="flex min-w-0 flex-1 flex-col"
            onKeyDown={(e) => {
              disarm(e.key)(e);
              onKeyDown?.(e);
            }}
          >
            <DialogHeader className="flex-row items-center justify-between gap-2 border-b border-border px-4 py-2.5">
              <DialogTitle>{title}</DialogTitle>
              {!busy && (
                <DialogClose
                  render={
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      className={cn(
                        "-mr-1.5",
                        armed && "bg-destructive/10 text-destructive hover:bg-destructive/20",
                      )}
                    />
                  }
                  aria-label={armed ? "Close and discard changes" : "Close"}
                  title={armed ? "Press again to discard" : "Close"}
                >
                  <XIcon />
                </DialogClose>
              )}
            </DialogHeader>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">{children}</div>
            {(footer || armed) && (
              <DialogFooter className="mx-0 mb-0 items-center gap-2 rounded-none px-4 py-3 sm:flex-row">
                {armed && (
                  <span
                    role="status"
                    className="mr-auto text-xs text-muted-foreground"
                  >
                    Unsaved changes — close again to discard
                  </span>
                )}
                {footer}
              </DialogFooter>
            )}
          </div>
          {side && (
            <aside className="flex w-[26rem] shrink-0 flex-col border-l border-border motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-right-2 motion-safe:duration-200">
              {side}
            </aside>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
