// Shared form-field primitives — thin, typed wrappers over the shadcn Field /
// Input / Textarea primitives, keeping the exports every form already imports
// (`Field`, `TextInput`, `Select`, `TextArea`) so callers get the new look
// without changes.
//
// Refs are forwarded to the underlying element on purpose: React 18 drops `ref`
// from a plain function component at runtime even though the shadcn wrappers'
// types accept it, and the Modal chassis' `initialFocus` points at these.
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
import { ChevronDownIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import {
  Field as ShadField,
  FieldDescription,
  FieldLabel,
  FieldTitle,
} from "@platform/shadcn/ui/field";

// Caption + control column. Associates the caption with the control: pass
// `htmlFor` explicitly, or — when children is a single element without an id —
// one is generated (useId) and cloned onto it. Otherwise the caption renders as
// a plain (non-label) title, e.g. a button in the caption-aligned slot.
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
        <span className="text-destructive" title="required" aria-hidden="true">
          *
        </span>
      )}
    </>
  );
  return (
    <ShadField className="gap-1.5">
      {controlId ? (
        <FieldLabel htmlFor={controlId} className="text-xs text-muted-foreground">
          {caption}
        </FieldLabel>
      ) : (
        <FieldTitle className="text-xs font-medium text-muted-foreground">{caption}</FieldTitle>
      )}
      {content}
      {hint != null && <FieldDescription className="text-xs">{hint}</FieldDescription>}
    </ShadField>
  );
}

// The shadcn Input / Textarea / SelectTrigger class strings, applied to native
// elements so a ref reaches the DOM node.
const CONTROL =
  "w-full min-w-0 rounded-md border border-input bg-transparent text-sm transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:bg-input/30 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40";

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function TextInput({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        data-slot="input"
        className={cn(CONTROL, "h-8 px-2.5 py-1 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground", className)}
        {...props}
      />
    );
  },
);

// A NATIVE <select> (callers drive it with `value`/`onChange`/`<option>`), drawn
// like the shadcn SelectTrigger: platform chevron suppressed, a lucide one laid
// over the right edge.
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...props }, ref) {
    return (
      <span className="relative inline-flex min-w-0 max-w-full">
        <select
          ref={ref}
          data-slot="select-trigger"
          className={cn(CONTROL, "h-8 appearance-none py-1 pr-8 pl-2.5 dark:hover:bg-input/50", className)}
          {...props}
        />
        <ChevronDownIcon
          aria-hidden="true"
          className="pointer-events-none absolute top-1/2 right-2 size-4 -translate-y-1/2 text-muted-foreground"
        />
      </span>
    );
  },
);

export const TextArea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function TextArea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      data-slot="textarea"
      className={cn(CONTROL, "field-sizing-content min-h-16 px-2.5 py-2", className)}
      {...props}
    />
  );
});
