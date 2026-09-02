// Flow composites for status: a size-1.5 dot, an outlined/filled circle icon,
// and a tinted badge. All colour comes from status-colors.ts.
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { bucketOf, bucketBadge, bucketBorder, bucketFill, type StatusBucket } from "@platform/ui/status-colors";

type Props = { status?: string | null; bucket?: StatusBucket; label?: string; className?: string; pulse?: boolean };

function resolve(p: Props): StatusBucket {
  return p.bucket ?? bucketOf(p.status);
}

export function StatusDot(p: Props) {
  const b = resolve(p);
  return (
    <span
      data-slot="status-dot"
      role={p.label ? "img" : undefined}
      aria-label={p.label}
      aria-hidden={p.label ? undefined : true}
      className={cn("inline-block size-1.5 rounded-full shrink-0", bucketFill[b], p.pulse && "flow-pulse", p.className)}
    />
  );
}

export function StatusIcon(p: Props & { filled?: boolean }) {
  const b = resolve(p);
  return (
    <span
      data-slot="status-icon"
      role={p.label ? "img" : undefined}
      aria-label={p.label}
      aria-hidden={p.label ? undefined : true}
      className={cn(
        "inline-block size-3.5 rounded-full border-[1.5px] shrink-0",
        bucketBorder[b],
        p.filled && bucketFill[b],
        p.pulse && "flow-pulse",
        p.className,
      )}
    />
  );
}

export function StatusBadge({ status, bucket, children, className }: Props & { children?: React.ReactNode }) {
  const b = bucket ?? bucketOf(status);
  return (
    <Badge variant="secondary" className={cn("border-transparent", bucketBadge[b], className)}>
      {children ?? status}
    </Badge>
  );
}
