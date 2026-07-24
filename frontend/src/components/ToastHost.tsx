// The app-root host for the global toast store (lib/toast.ts): a fixed,
// bottom-centre stack rendered once by App so toasts appear over any view.
// Each entry reuses the presentational Toast banner; .toast-host in shell.css
// overrides Toast's own absolute positioning so multiple stack instead of
// piling on top of one another.
import Toast from "./Toast";
import { dismissToast, useToasts } from "../lib/toast";

export default function ToastHost() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;
  return (
    <div className="toast-host">
      {toasts.map((t) => (
        <Toast
          key={t.id}
          msg={t.msg}
          tone={t.tone}
          action={t.action}
          onClose={() => dismissToast(t.id)}
        />
      ))}
    </div>
  );
}
