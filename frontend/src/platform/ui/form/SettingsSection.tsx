// A section of a settings page: a heading with a hairline rule, then its
// rows. Replaces `.prefs-section` / `.prefs-section h2`.
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@platform/lib/utils";

export function SettingsSection({
  title,
  className,
  children,
  ...props
}: ComponentProps<"section"> & { title?: ReactNode }) {
  return (
    <section className={cn("flex flex-col gap-2.5", className)} {...props}>
      {title != null && (
        <h2 className="border-b border-border pb-1.5 text-body font-semibold">{title}</h2>
      )}
      {children}
    </section>
  );
}
