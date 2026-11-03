// The soft-block question (CONTRIB_LOOP.md §18.4 B4): Fused flagged the
// release this copy runs as one to be careful with — updates pause, and
// opening the app asks first. One sheet for both places that open an app:
// the /apps card (platform/ui/AppPreviewCard) and the app page's Source panel
// (shell/contrib/BlockedStrip). "Open anyway" posts the server's
// `confirm-block` action (`blocks.confirm_soft`), after which the app opens
// without asking again.
//
// In platform, not shell/contrib, because the card is platform UI and may
// not import the shell.
import { useState } from "react";
import { Loader2 } from "lucide-react";
import { plainMessage, runAction } from "@platform/lib/contrib";
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

/** The server's action id for "Open anyway". */
export const CONFIRM_BLOCK_ID = "confirm-block";

export interface SoftBlockFacts {
  reason: string;
  what_to_do: string;
}

/** Say yes to a soft block for the app at `dir`. Resolves with a plain
 *  sentence on a refusal, null on success. */
export async function confirmSoftBlock(dir: string): Promise<string | null> {
  const r = await runAction(dir, CONFIRM_BLOCK_ID, { confirm: true });
  return r.ok ? null : plainMessage(r);
}

export function SoftBlockConfirm({
  open,
  dir,
  name,
  block,
  onClose,
  onConfirmed,
}: {
  open: boolean;
  dir: string;
  name: string;
  block: SoftBlockFacts;
  onClose: () => void;
  /** Runs after the server took the yes (open the app, re-read the panel). */
  onConfirmed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <AlertDialog
      open={open}
      onOpenChange={(o) => {
        if (!o && !busy) {
          setError(null);
          onClose();
        }
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Open {name} anyway?</AlertDialogTitle>
          <AlertDialogDescription>
            Fused flagged the version of this app you have: {block.reason}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {block.what_to_do && <p className="contrib-block-todo">{block.what_to_do}</p>}
        <p className="contrib-block-note">Updates for this app are paused until Fused clears it.</p>
        {error && (
          <p className="contrib-error" role="alert">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Not now</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              const said = await confirmSoftBlock(dir);
              setBusy(false);
              if (said) return setError(said);
              onClose();
              onConfirmed();
            }}
          >
            {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
            Open anyway
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
