// Small visual vocabulary shared by the Mounts page, its list and the setup
// dialogs: a one-line note (plain / ok / warn), a callout (Alert with a title
// and body), and the panel scaffolding every setup flow opens with.
import type { ComponentProps, ReactNode } from "react";
import { AlertTriangleIcon, InfoIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@platform/shadcn/ui/alert";
import { bucketText } from "@platform/ui/status-colors";

/**
 * A muted explainer line. `tone` colours it via status-colors: "ok" for a
 * handoff that landed, "warn" for a caveat the reader must not skim past.
 */
export function Note({
  tone = "plain",
  className,
  children,
  ...rest
}: {
  tone?: "plain" | "ok" | "warn";
  className?: string;
  children: ReactNode;
  role?: string;
  title?: string;
}) {
  return (
    <p
      className={cn(
        "text-xs leading-snug",
        tone === "plain" && "text-muted-foreground",
        tone === "ok" && bucketText.green,
        tone === "warn" && bucketText.orange,
        className,
      )}
      {...rest}
    >
      {children}
    </p>
  );
}

/** Inline code inside a note or description. */
export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn("font-mono text-xs bg-muted px-1 py-px rounded-sm break-all", className)}>{children}</code>;
}

/** A titled callout. `warn` swaps the glyph and tints the title. */
export function Callout({
  title,
  warn,
  action,
  children,
  className,
}: {
  title: ReactNode;
  warn?: boolean;
  action?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <Alert className={cn("shadow-sm", className)}>
      {warn ? <AlertTriangleIcon className={bucketText.orange} /> : <InfoIcon className="text-muted-foreground" />}
      <AlertTitle className={cn(warn && bucketText.orange)}>{title}</AlertTitle>
      {children != null && (
        <AlertDescription className="[&_a]:underline [&_a]:underline-offset-3 [&_p]:m-0">{children}</AlertDescription>
      )}
      {action != null && <div className="col-start-2 mt-2 flex gap-2">{action}</div>}
    </Alert>
  );
}

/** The body of a setup dialog: lede sentence, then whatever the flow needs. */
export function Panel({ lede, children }: { lede: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-3 text-sm">
      <p className="text-sm text-foreground">{lede}</p>
      {children}
    </div>
  );
}

/** A quiet inline text button ("Use a different client", "Forget the saved client"). */
export function LinkButton({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(
        "text-xs text-muted-foreground underline underline-offset-3 hover:text-foreground rounded-sm focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
        className,
      )}
      {...props}
    />
  );
}
