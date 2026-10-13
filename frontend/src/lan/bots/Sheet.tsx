// A bottom sheet on a phone (centred card on a wide screen), built on the same
// base-ui Dialog the shadcn dialog wraps: focus trap, Escape, backdrop tap and
// the title for screen readers come with it. The body scrolls; the footer (the
// sheet's one action) stays put above the home indicator.
import type { ReactNode } from "react";
import { Dialog as D } from "@base-ui/react/dialog";
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";

export function Sheet({
  open,
  onOpenChange,
  title,
  children,
  footer,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  return (
    <D.Root open={open} onOpenChange={(o) => onOpenChange(o)}>
      <D.Portal>
        <D.Backdrop className="fixed inset-0 z-50 bg-black/40 duration-150 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <D.Popup
          className={cn(
            "fixed inset-x-0 bottom-0 z-50 mx-auto flex max-h-[92dvh] w-full max-w-lg flex-col rounded-t-2xl border border-b-0 border-[var(--sh-border)] bg-[var(--bg-popover,var(--card))] text-[var(--fg)] shadow-2xl outline-none duration-200 data-open:animate-in data-open:slide-in-from-bottom data-closed:animate-out data-closed:slide-out-to-bottom",
            className,
          )}
        >
          <div className="mx-auto mt-2 h-1 w-9 shrink-0 rounded-full bg-[var(--fg-muted)] opacity-30" aria-hidden />
          <div className="flex shrink-0 items-center justify-between gap-2 px-4 pt-1 pb-2">
            <D.Title className="text-[17px] font-semibold">{title}</D.Title>
            <D.Close
              aria-label="Close"
              className="-mr-2 flex size-11 items-center justify-center rounded-full text-[var(--fg-muted)] active:bg-[var(--row-bg-hover)]"
            >
              <XIcon className="size-5" />
            </D.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-4">{children}</div>
          {footer && (
            <div className="shrink-0 border-t border-[var(--sh-border)] px-4 pt-3 pb-[max(12px,env(safe-area-inset-bottom))]">
              {footer}
            </div>
          )}
          {!footer && <div className="shrink-0 pb-[env(safe-area-inset-bottom)]" />}
        </D.Popup>
      </D.Portal>
    </D.Root>
  );
}

/** A labelled field row inside a sheet. */
export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="flex flex-col gap-1.5 py-2">
      <label htmlFor={htmlFor} className="text-[13px] font-medium text-[var(--fg-muted)]">
        {label}
      </label>
      {children}
      {hint && <div className="text-[12px] leading-snug text-[var(--fg-muted)]">{hint}</div>}
    </div>
  );
}

/** Section heading inside a sheet. */
export function SheetSection({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="border-t border-[var(--sh-border)] pt-3 mt-3 first:mt-0 first:border-t-0 first:pt-0">
      <div className="flex items-center justify-between">
        <h3 className="lan-mono text-[11px] font-medium tracking-[0.08em] text-[var(--fg-muted)] uppercase">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}
