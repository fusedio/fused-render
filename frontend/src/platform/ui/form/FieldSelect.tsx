// The app's one select chrome (what `.field-control` drew: 32px, 6px radius,
// page background, hairline border, focus = border to fg-muted, no ring) on
// top of the shadcn NativeSelect — so every page's select is the same select,
// and it stays a native <select> (options, keyboard, unknown values) underneath.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";
import { NativeSelect } from "@platform/shadcn/ui/native-select";

export { NativeSelectOption as FieldSelectOption } from "@platform/shadcn/ui/native-select";

export function FieldSelect({ className, ...props }: ComponentProps<typeof NativeSelect>) {
  return (
    <NativeSelect
      className={cn(
        "[&>select]:h-8 [&>select]:rounded-control [&>select]:border-border [&>select]:bg-background [&>select]:pl-2.5 [&>select]:text-dense [&>select]:leading-[normal]",
        "[&>select]:dark:bg-background [&>select]:dark:hover:bg-background",
        "[&>select]:focus-visible:border-[var(--fg-muted)] [&>select]:focus-visible:ring-0",
        className,
      )}
      {...props}
    />
  );
}
