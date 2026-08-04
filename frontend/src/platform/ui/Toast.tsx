// One transient notification (an error, or a non-red "info" confirmation).
// Purely presentational: the global store (lib/toast) owns the queue and the
// auto-dismiss timer, and NotificationHost owns where it sits, so this renders
// only the banner + optional action + dismiss button. Styling is .toast* in
// shell.css.
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
      className={"toast" + (tone === "info" ? " toast-info" : "") + (leaving ? " toast-leaving" : "")}
      role={tone === "info" ? "status" : "alert"}
      aria-hidden={leaving || undefined}
    >
      <span className="toast-msg">{msg}</span>
      {action && (
        <button
          type="button"
          className="toast-action"
          disabled={leaving}
          onClick={action.onClick}
        >
          {action.label}
        </button>
      )}
      <button
        type="button"
        className="toast-close"
        onClick={onClose}
        disabled={leaving}
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
}
