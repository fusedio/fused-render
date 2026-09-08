// The app page's shared "this commit failed to resolve" state — one banner,
// used by all three tabs a `_snapshot` can gate (Overview, Files, API; code
// review finding 1, second round). Before this, a transient failure
// (`useAppPageSnapshot`'s `error`) left every one of them on an indefinite
// skeleton: `pending` alone cannot tell a caller "still resolving" apart
// from "will never resolve without help", and nothing retried on its own
// (the resolve effect only re-runs on `dir`/`urlVersion`). This gives the
// user two real ways out — try the same commit again, or drop back to Live
// — rather than leaving the version picker as the only (undiscoverable)
// escape.
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { replaceSearch } from "@platform/lib/router";
import { SNAPSHOT_PARAM } from "./AppVersionPicker";

export default function SnapshotError({ onRetry }: { onRetry: () => void }) {
  const backToLive = () => {
    const params = new URLSearchParams(location.search);
    params.delete(SNAPSHOT_PARAM);
    const q = params.toString();
    replaceSearch(location.pathname + (q ? "?" + q : ""));
  };
  return (
    <ErrorBanner>
      <p className="m-0">
        Could not load this commit. This may be a temporary problem.
      </p>
      <div className="flex gap-2 pt-2">
        <Button size="xs" variant="outline" onClick={onRetry}>
          Retry
        </Button>
        <Button size="xs" variant="ghost" onClick={backToLive}>
          Back to Live
        </Button>
      </div>
    </ErrorBanner>
  );
}
