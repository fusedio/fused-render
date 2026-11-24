// The modal a Connect press opens on a sign-in that will trip the browser's
// certificate warning (an https localhost redirect — Slack, connect/tls.py):
// two short lines, a "Why?" disclosure for whoever wants the reasoning, and
// Continue / Cancel. Shared by Preferences › Connections and the bot Settings
// tab (owner, 2026-11-04: a popover, not a note inside the card; keep the
// main text short).
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";

export const CERT_NOTICE_SHORT =
  "Your browser will warn that the page is not secure when it returns here. That is this app, not an attacker: choose Advanced › Proceed.";

export const CERT_NOTICE_WHY =
  "Slack lets a distributed app return only to an https address, so the sign-in comes back to https://localhost on this " +
  "computer, where this app answers with a certificate it made for itself; no authority vouches for it, hence the warning. " +
  "A Fused-run relay would avoid it, but then a Fused server would see your sign-in. Keeping it local was the choice. " +
  "If you would rather not click through, paste the address the warning page shows into the card instead.";

export function ConnectCertNotice({ open, name, busy, onContinue, onCancel }: {
  open: boolean;
  /** The service's name ("Slack"). */
  name: string;
  busy?: boolean;
  onContinue: () => void;
  onCancel: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={(o) => { if (!o) onCancel(); }}>
      <AlertDialogContent data-cert-notice="1">
        <AlertDialogHeader>
          <AlertDialogTitle>Sign in to {name}</AlertDialogTitle>
          <AlertDialogDescription>{CERT_NOTICE_SHORT}</AlertDialogDescription>
        </AlertDialogHeader>
        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer select-none">Why?</summary>
          <p className="mt-2 leading-6">{CERT_NOTICE_WHY}</p>
        </details>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction disabled={busy} onClick={onContinue}>Continue to {name}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
