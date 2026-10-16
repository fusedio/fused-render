// The sign-in step on its own sheet, for the card's one-click actions that
// GitHub refused because this computer is signed out (Update proposal,
// Withdraw, Update directly): "Sign in to GitHub to <do it>." → the shared
// sign-in step → the action runs again, once.
//
// Always `recheck`: the sheet only opens after the server said "signed
// out", so a cached "signed in" from before is exactly the answer not to
// trust — without it the step would advance at once and the retry would
// refuse again, round and round.
import { GitHubSignIn } from "@platform/ui/GitHubSignIn";
import { ContribDialog } from "./ContribDialog";

export function SignInSheet({
  doing,
  onSignedIn,
  onClose,
}: {
  /** What the retry does, finishing "Sign in to GitHub to …". */
  doing: string;
  onSignedIn: () => void;
  onClose: () => void;
}) {
  return (
    <ContribDialog
      open
      onClose={onClose}
      title="Sign in to GitHub"
      description={`GitHub needs you signed in to ${doing}. It picks up where you left off.`}
    >
      <GitHubSignIn recheck prompt={`Sign in to GitHub to ${doing}.`} onSignedIn={onSignedIn} onCancel={onClose} />
    </ContribDialog>
  );
}
