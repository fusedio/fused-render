// One toast at a time, bottom-centre above the composer: a refused send's
// sentence (409), a failed call, "Context cleared". A module store so any
// component can call toast() without a provider.
import { useSyncExternalStore } from "react";
import { cn } from "@platform/lib/utils";

type ToastState = { id: number; text: string; tone: "info" | "error" } | null;
let state: ToastState = null;
let timer: ReturnType<typeof setTimeout> | null = null;
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());

export function toast(text: string, tone: "info" | "error" = "info") {
  state = { id: (state?.id ?? 0) + 1, text, tone };
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    state = null;
    emit();
  }, tone === "error" ? 5000 : 2600);
  emit();
}

export function Toaster({ bottom = 24 }: { bottom?: number }) {
  const t = useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => state,
  );
  if (!t) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 z-[70] flex justify-center px-4"
      style={{ bottom: `calc(${bottom}px + env(safe-area-inset-bottom))` }}
      role="status"
      aria-live="polite"
    >
      <div
        key={t.id}
        onClick={() => {
          state = null;
          emit();
        }}
        className={cn(
          "pointer-events-auto max-w-md rounded-2xl px-4 py-3 text-[15px] leading-snug shadow-lg duration-200 animate-in fade-in-0 slide-in-from-bottom-2",
          t.tone === "error"
            ? "bg-[var(--error)] text-white"
            : "bg-[var(--fg)] text-[var(--bg)]",
        )}
      >
        {t.text}
      </div>
    </div>
  );
}
