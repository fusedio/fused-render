// The page's error strip: a destructive shadcn Alert. Same contract as the
// platform ErrorBanner it replaces here — null/false children draw nothing —
// so call sites can pass `{error}` straight through.
import type { ReactNode } from "react";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";

export function ErrorNote({ children }: { children: ReactNode }) {
  if (children == null || children === false) return null;
  return (
    <Alert variant="destructive">
      <AlertDescription className="text-destructive">{children}</AlertDescription>
    </Alert>
  );
}
