// A quiet checkbox with the label that names it. Used twice — Repeat, and the
// "fresh task each run" flag behind it. The shadcn Checkbox is the real control
// (focusable, space-toggled, announced as a checkbox); `describedBy` attaches a
// hint printed elsewhere to the control itself, so a screen reader reads the
// consequence with the box rather than as a stray line below it.
import { useId } from "react";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Label } from "@platform/shadcn/ui/label";
import { cn } from "@platform/lib/utils";

export function CheckRow({
  checked,
  onChange,
  label,
  className,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  className?: string;
  describedBy?: string;
}) {
  const id = useId();
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <Checkbox
        id={id}
        checked={checked}
        aria-describedby={describedBy}
        onCheckedChange={(next) => onChange(next)}
      />
      <Label htmlFor={id} className="cursor-pointer text-sm font-normal">
        {label}
      </Label>
    </div>
  );
}
