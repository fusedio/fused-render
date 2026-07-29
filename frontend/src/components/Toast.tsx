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
  onClose,
}: {
  msg: string;
  tone: ToastTone;
  action?: ToastAction;
  onClose: () => void;
}) {
  return (
    <div
      className={"toast" + (tone === "info" ? " toast-info" : "")}
      role={tone === "info" ? "status" : "alert"}
    >
      <span className="toast-msg">{msg}</span>
      {action && (
        <button
          type="button"
          className="toast-action"
          onClick={action.onClick}
        >
          {action.label}
        </button>
      )}
      <button
        type="button"
        className="toast-close"
        onClick={onClose}
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
}
