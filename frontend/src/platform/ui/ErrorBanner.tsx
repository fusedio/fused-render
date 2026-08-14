// Shared error card: a role="alert" region with a full 1px error-tinted border
// and a subtle error-tinted background (no left stripe). Replaces the ad-hoc
// .deploy-error divs scattered across the modals/forms.
import type { ReactNode } from "react";

export function ErrorBanner({ children }: { children: ReactNode }) {
  if (children == null || children === false) return null;
  return (
    <div className="error-banner deploy-error" role="alert">
      {children}
    </div>
  );
}

export default ErrorBanner;
