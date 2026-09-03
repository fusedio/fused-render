// Shared error card: a role="alert" region in the destructive Alert variant.
// Replaces the ad-hoc .deploy-error divs scattered across the modals/forms.
import type { ReactNode } from "react";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";

export function ErrorBanner({ children }: { children: ReactNode }) {
  if (children == null || children === false) return null;
  return (
    <Alert variant="destructive" className="border-destructive/40">
      <AlertDescription className="max-h-44 overflow-y-auto whitespace-pre-wrap break-words">
        {children}
      </AlertDescription>
    </Alert>
  );
}

export default ErrorBanner;
