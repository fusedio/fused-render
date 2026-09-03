// The floating notification column: one fixed, bottom-right stack holding
// every transient toast (lib/toast) and the server-health card. Mounted once
// by App, alongside `StatusBar` (platform/ui/StatusBar.tsx).
//
// It replaced three competing surfaces — a bottom-centre global toast stack, a
// per-pane toast each of Listing and Preview positioned and expired itself,
// and this bottom-right card — which between them meant the same "Path copied"
// appeared in two different places depending on which view raised it, and a
// toast could sit next to (or under) an unrelated card in the other corner.
// One stack, one set of stacking rules, one auto-dismiss timer (the store's).
//
// Order is oldest → newest top to bottom, so the newest message is nearest the
// bottom edge where the eye already is, and the server card sits below all of
// them: it is the one entry that outlives any toast, so it must not shuffle as
// toasts come and go.
//
// TWO ENTRIES USED TO LIVE HERE AND DO NOT ANY MORE (D563, status bar
// redesign): the activity card and the repo-updates card, both LONG-LIVED.
// `StatusBar` hosts them now, inside `#main`, where collapsing them actually
// gives the page its space back. What stays here — toasts, `ServerStatusBanner`
// — is either seconds-long or exceptional enough that overlaying the page is
// the right call for it.
//
// Panes keep their attribution for free: in panel/tab mode each pane is its
// own document, so a pane's toast renders in THAT pane's bottom-right corner,
// not the window's. Only the top-level document shows the server card (an
// embed would otherwise render one per pane, all saying the same thing).
//
// pointer-events: none on the column so its empty region never swallows
// clicks on the view beneath; each entry takes them back. z-index above the
// dialog layer (z-50): a toast with an action stays usable over an open modal.
import { cn } from "@platform/lib/utils";
import ServerStatusBanner from "@platform/ui/ServerStatusBanner";
import Toast from "@platform/ui/Toast";
import { dismissToast, useToasts } from "@platform/lib/toast";
import { IS_EMBED } from "@platform/lib/router";

export default function NotificationHost() {
  const toasts = useToasts();
  return (
    <div
      data-slot="notification-host"
      className="pointer-events-none fixed right-4 bottom-4 z-[2000] flex w-[min(360px,calc(100vw-2rem))] flex-col items-stretch gap-2 *:pointer-events-auto"
    >
      {/* Each toast rides in a grid-row wrapper whose row collapses 1fr → 0fr
          on the way out (plus a negative bottom margin that eats the column's
          gap), so the cards below it GLIDE up instead of snapping the moment
          one is dismissed. The wrapper is what animates height; the card itself
          only fades and slides (Toast.tsx). lib/toast keeps a dismissed toast
          in the queue for TOAST_EXIT_MS — the 150ms these transitions run for. */}
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "grid max-w-full transition-[grid-template-rows,margin-bottom] duration-150 motion-reduce:transition-none",
            t.leaving ? "-mb-2 [grid-template-rows:0fr]" : "[grid-template-rows:1fr]",
          )}
        >
          <Toast
            msg={t.msg}
            tone={t.tone}
            action={t.action}
            leaving={t.leaving}
            onClose={() => dismissToast(t.id)}
          />
        </div>
      ))}
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
