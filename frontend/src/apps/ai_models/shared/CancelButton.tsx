// The download manager's own ✕, for any row on /ai-models whose model is being
// fetched right now.
//
// Shared because every model row needs it and for one reason: a multi-GB
// download the user changed their mind about must be stoppable from the row
// that started it, wherever that row is (D440).
import { XIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { isRunning, type Job } from "@platform/lib/jobs";

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
