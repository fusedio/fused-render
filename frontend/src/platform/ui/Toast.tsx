// One transient notification (an error, or a non-red "info" confirmation).
// Purely presentational: the global store (lib/toast) owns the queue and the
// auto-dismiss timer, and NotificationHost owns where it sits, so this renders
// only the card + optional action + dismiss button. Tone is said by the status
// dot (status-colors: red = error, green = a confirmation), not by the border.
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { StatusDot } from "@platform/ui/flow/StatusIcon";

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
      data-slot="toast"
      className={cn(
        "flex min-h-0 max-w-full items-center gap-2.5 overflow-hidden rounded-lg border border-border bg-popover py-2 pr-2 pl-3 text-sm text-popover-foreground shadow-sm",
        "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-2 motion-safe:duration-150",
        leaving && "translate-y-1 opacity-0 transition-[opacity,transform] duration-150 motion-safe:animate-none motion-reduce:transition-none",
      )}
      role={tone === "info" ? "status" : "alert"}
      aria-hidden={leaving || undefined}
    >
      <StatusDot bucket={tone === "error" ? "red" : "green"} />
      {/* Wraps instead of ellipsising — these are sentences whose second half
          says what to do — clamped so a pathological one can't own the screen. */}
      <span className="line-clamp-4 min-w-0 flex-1 leading-snug">{msg}</span>
      {action && (
        <Button type="button" variant="outline" size="xs" disabled={leaving} onClick={action.onClick}>
          {action.label}
        </Button>
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        onClick={onClose}
        disabled={leaving}
        aria-label="Dismiss"
      >
        <XIcon />
      </Button>
    </div>
  );
}
