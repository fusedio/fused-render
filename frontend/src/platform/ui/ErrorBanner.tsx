// Shared error card: a role="alert" region in the destructive Alert variant.
// Replaces the ad-hoc .deploy-error divs scattered across the modals/forms.
import type { ReactNode } from "react";
import { cn } from "@platform/lib/utils";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";

// `className` is for the caller's PLACEMENT of the banner, not its look: a page
// that used to reach in with `[&>.error-banner]:m-4` (AppFiles) has no class to
// select on any more, so it passes the margin instead.
export function ErrorBanner({ children, className }: { children: ReactNode; className?: string }) {
  if (children == null || children === false) return null;
  return (
    <Alert variant="destructive" className={cn("border-destructive/40", className)}>
      <AlertDescription className="max-h-44 overflow-y-auto whitespace-pre-wrap break-words">
        {children}
      </AlertDescription>
    </Alert>
  );
}

export default ErrorBanner;
