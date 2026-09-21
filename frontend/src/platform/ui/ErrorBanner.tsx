// Shared error card: a role="alert" region with a full 1px error-tinted border
// and a subtle error-tinted background (no left stripe). Replaces the ad-hoc
// .deploy-error divs scattered across the modals/forms.
import type { ReactNode } from "react";
import { Sparkles } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";

export function ErrorBanner({
  children,
  onExplain,
}: {
  children: ReactNode;
  // A "Explain with AI" action (SPEC-doctor-git-ai-errors.md Part A) —
  // opt-in, because this component has no idea what kind of error its
  // children describe. A call site passes this for a system/runtime error
  // (an unexpected failure, or a curated one with a written remedy); a
  // call site showing a plain input-validation message ("name is
  // required") must never pass it — there's nothing for Claude to explain
  // that the message doesn't already say. The handler itself only opens a
  // chat seeded with the error (platform/lib/explain-with-ai.ts) — a
  // single click here must never start edits.
  onExplain?: () => void;
}) {
  if (children == null || children === false) return null;
  return (
    <div className="error-banner deploy-error" role="alert">
      {children}
      {onExplain && (
        <div className="flex gap-2 pt-2">
          <Button size="xs" variant="ghost" onClick={onExplain}>
            <Sparkles data-icon="inline-start" />
            Explain with AI
          </Button>
        </div>
      )}
    </div>
  );
}

export default ErrorBanner;
