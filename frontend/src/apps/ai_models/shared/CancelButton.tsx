// The download manager's own ✕, for any card on /ai-models whose model is being
// fetched right now.
//
// Shared because all three card kinds need it and for one reason: a multi-GB
// download the user changed their mind about must be stoppable from the card
// that started it, wherever that card is. It lived in `RecommendedCard` while
// only the not-on-disk cards had it, and a partly downloaded repo card was left
// with a disabled "Downloading…" button and no way to stop the pull at all
// (D440) — which on a 40GB fetch is the difference between a mistake and a
// mistake you have to wait out.
import { XIcon } from "lucide-react";
import { isRunning, type Job } from "@platform/lib/jobs";
import { Button } from "@platform/shadcn/ui/button";

export function CancelButton({
  id,
  job,
  onCancel,
}: {
  id: string;
  job: Job | undefined;
  onCancel: (job: Job) => void;
}) {
  // The download manager's own rule and not a looser one: a running job its
  // reporter never marked cancellable gets NO ✕ rather than a dead one, and a
  // cancel already asked for is not asked again.
  const cancellable =
    job && isRunning(job) && job.cancellable && !job.cancel_requested && !job.stalled ? job : null;
  if (!cancellable) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      title={`Stop downloading ${id}`}
      aria-label={`Stop downloading ${id}`}
      onClick={() => onCancel(cancellable)}
    >
      <XIcon />
    </Button>
  );
}
