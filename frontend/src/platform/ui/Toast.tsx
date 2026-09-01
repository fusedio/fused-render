// One transient notification (an error, or a non-red "info" confirmation).
// Purely presentational: the global store (lib/toast) owns the queue and the
// auto-dismiss timer, and NotificationHost owns where it sits, so this renders
// only the banner + optional action + dismiss button. Since the shadcn
// migration the card is drawn in the shared component vocabulary (Button +
// semantic tokens); the slot glide animation stays in notifications.css.
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { XIcon } from "lucide-react";

export type ToastTone = "error" | "info";

// An optional call-to-action rendered before the dismiss button (e.g. the
// mount-health "Reconnect" affordance). The host owns what onClick does.
export interface ToastAction {
  label: string;
  onClick: () => void;
}

export default function Toast({
  msg,
  tone,
  action,
  leaving = false,
  onClose,
}: {
  msg: string;
  tone: ToastTone;
  action?: ToastAction;
  // Dismissed and playing its exit animation (lib/toast keeps it in the queue
  // for that long). Its controls are inert while it leaves — a click landing on
  // a card that is already fading out should do nothing.
  leaving?: boolean;
  onClose: () => void;
}) {
  return (
    <div
      className={cn(
        "toast flex items-center gap-2 rounded-lg bg-popover py-2 pr-2 pl-3 text-sm shadow-md ring-1",
        tone === "error" ? "text-destructive ring-destructive/40" : "text-foreground ring-foreground/10",
        leaving && "toast-leaving",
      )}
      role={tone === "info" ? "status" : "alert"}
      aria-hidden={leaving || undefined}
    >
      <span className="min-w-0 flex-1">{msg}</span>
      {action && (
        <Button variant="outline" size="sm" disabled={leaving} onClick={action.onClick}>
          {action.label}
        </Button>
      )}
      <Button variant="ghost" size="icon-sm" onClick={onClose} disabled={leaving} aria-label="Dismiss">
        <XIcon />
      </Button>
    </div>
  );
}
