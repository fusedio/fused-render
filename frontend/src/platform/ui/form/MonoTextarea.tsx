// A monospace `Textarea` — replaces `.prefs-textarea` (skip-folders list) and
// `.index-query-input` (the SQL/Ask box, `noWrap`: no line-wrap, scrolls like
// an editor instead).
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";
import { Textarea } from "@platform/shadcn/ui/textarea";

export function MonoTextarea({
  className,
  noWrap,
  ...props
}: ComponentProps<typeof Textarea> & { noWrap?: boolean }) {
  return (
    <Textarea
      className={cn(
        "font-mono text-meta leading-[1.5]",
        noWrap && "overflow-x-auto whitespace-pre",
        className,
      )}
      {...props}
    />
  );
}
