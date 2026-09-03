// "Copy" that says "Copied" for two seconds. Shared by the TroubleCard and the
// Claude health strip (both used to carry their own copy of it), and by the
// command plate below, which is the "here is a line to run" chrome those two
// surfaces present identically.
import { useState, type ReactNode } from "react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";

export function CopyButton({
  text,
  label = "Copy",
  className,
}: {
  text: string;
  label?: ReactNode;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={className}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          // No clipboard permission. Saying nothing would leave the user
          // pressing it again forever; the text is on screen either way.
          return;
        }
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      }}
    >
      {copied ? "Copied" : label}
    </Button>
  );
}

// A command to run, verbatim, beside its copy button.
export function CommandPlate({ command, className }: { command: string; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <code className="min-w-0 rounded-md border border-border bg-background px-2 py-1 font-mono text-xs break-all">
        {command}
      </code>
      <CopyButton text={command} />
    </div>
  );
}

export default CopyButton;
