// The floating pop-up half of a client-raised message (SPEC
// toasts-become-notifications §2) — the message counterpart to
// `JobPopupCard.tsx`. It draws the same `NotificationCard` the Notifications
// panel draws for this message's own retained row (if it has one), not the
// old `Toast` — same rule, same reason, as `JobPopupCard.tsx`'s own header
// comment on this point.
//
// UNLIKE `JobPopupCard`, this card does NOT own its own visible/exit timers.
// `lib/notifications.ts`'s `notify()` already arms them itself (so the store
// behaves correctly even with nothing mounted to render it — see
// `notifications.test.ts`, which drives the whole pop→leaving→gone sequence
// with no React tree at all). Re-arming a SECOND, independent timer here
// would race the store's own and could unmount this card on one clock while
// the store still thought its popup was live on the other. So this card is
// purely reactive to `notification.leaving`, and only adds the two behaviours
// that have no server-timer equivalent: outside-press dismissal and the
// iframe-blur edge case, both funnelled through `dismissPopup()` — the same
// single exit path the ✕ uses — via `usePopupCardLifecycle`'s `visibleMs:
// null` (skips ITS mount timer) and a no-op `onGone` (this card unmounts on
// its own once `useNotificationPopup()` goes null, it needs no callback for
// that).
//
// THE ✕ ONLY CLOSES THE CARD — calls `dismissPopup()`, never
// `dismissNotification()`. A message with nothing retained
// (`transient`/`silent`) loses nothing either way; an `attention`/`trail`
// message stays in the panel for the user to act on later.
//
// `IS_TOP_EMBED` NEVER AUTO-EXPIRES AN ATTENTION CARD (SPEC §4): a tab or
// bookmark opened standalone is its own top window, with no shell underneath
// it to retain the message for — the Notifications panel is behind
// `App.tsx`'s `!IS_EMBED` guard. `notify()` itself skips arming the exit
// timer for that one case (see its own comment), so there is nothing extra
// for this component to do here beyond not fighting that decision.
import { useRef } from "react";
import NotificationCard from "@platform/ui/NotificationCard";
import { usePopupCardLifecycle } from "@platform/ui/usePopupCardLifecycle";
import { dismissPopup, useNotificationPopup } from "@platform/lib/notifications";

const NOOP = () => {};

export default function MessagePopupCard() {
  const notification = useNotificationPopup();
  const cardRef = useRef<HTMLDivElement | null>(null);

  // Effects inside the hook are conditioned on `leaving`/`cardRef`, both
  // safe to call even while `notification` is null (they simply have
  // nothing to attach to / always take the "not leaving" branch). Keeping
  // the hook call unconditional avoids a hooks-order violation on the
  // popup's own null → non-null → null transitions.
  usePopupCardLifecycle({
    cardRef,
    leaving: notification?.leaving ?? false,
    setLeaving: () => dismissPopup(),
    onGone: NOOP,
    visibleMs: null,
    exitMs: 0,
  });

  if (!notification) return null;

  return (
    <div
      ref={cardRef}
      className={"toast-slot" + (notification.leaving ? " leaving" : "")}
    >
      <NotificationCard
        title={notification.title}
        secondary={notification.detail}
        terminal={notification.tone === "error" ? "error" : undefined}
        role={notification.tone === "error" ? "alert" : "status"}
        navAction={notification.action}
        onDismiss={{ onClick: () => dismissPopup() }}
      />
    </div>
  );
}
