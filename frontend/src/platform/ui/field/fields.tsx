// Shared form-field primitives — thin, typed wrappers whose rendering is the
// shadcn field vocabulary (@platform/shadcn/ui/field, input, textarea). The
// API is unchanged so the remaining consumers keep working; new code should
// compose the shadcn primitives directly.
import {
  cloneElement,
  forwardRef,
  isValidElement,
  useId,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import {
  Field as ShadField,
  FieldDescription,
  FieldLabel,
} from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { cn } from "@platform/lib/utils";
import { ChevronDownIcon } from "lucide-react";

// Caption + control column. Associates the caption with the control: pass
// `htmlFor` explicitly, or — when children is a single element without an
// id — one is generated (useId) and cloned onto it. Otherwise the caption
// renders as a plain span (e.g. a button in the caption-aligned slot).
export function Field({
  label,
  required,
  hint,
  htmlFor,
  children,
}: {
  label: ReactNode;
  required?: boolean;
  hint?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
}) {
  const autoId = useId();
  let controlId = htmlFor;
  let content = children;
  if (!controlId && isValidElement(children)) {
    const el = children as ReactElement<{ id?: string }>;
    controlId = el.props.id ?? autoId;
    if (!el.props.id) content = cloneElement(el, { id: controlId });
  }
  const caption = (
    <>
      {label}
      {required && (
        <span className="text-primary" title="required" aria-hidden="true">
          {" "}
          *
        </span>
      )}
    </>
  );
  return (
    <ShadField>
      {controlId ? (
        <FieldLabel htmlFor={controlId}>{caption}</FieldLabel>
      ) : (
        <span className="text-xs font-medium text-muted-foreground">{caption}</span>
      )}
      {content}
      {hint != null && <FieldDescription>{hint}</FieldDescription>}
    </ShadField>
  );
}

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function TextInput(props, ref) {
    return <Input ref={ref} {...props} />;
  },
);

// Native <select> kept for dense bars where the platform picker is the
// right tool; drawn in the Input's chrome with our own chevron so it sits
// flush with sibling inputs.
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...props }, ref) {
    return (
      <span className="relative inline-flex min-w-0 max-w-full">
        <select
          ref={ref}
          className={cn(
            "h-8 min-w-0 max-w-full appearance-none rounded-lg border border-input bg-transparent pr-7 pl-2.5 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50",
            className,
          )}
          {...props}
        />
        <ChevronDownIcon
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-muted-foreground"
        />
      </span>
    );
  },
);

export const TextArea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function TextArea(props, ref) {
  return <Textarea ref={ref} {...props} />;
});
