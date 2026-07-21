// Shared form-field primitives — thin, typed wrappers over the canonical field
// CSS (shell.css: .field / .field-label / .field-control). No new visual
// language: .field-control is exactly the `.modal-body :where(input, select,
// textarea)` style, extracted so native controls (notably <select>, which
// otherwise keeps its platform chevron/metrics) render identically everywhere.
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

const cx = (...parts: (string | undefined)[]) => parts.filter(Boolean).join(" ");

// Caption + control column (6px gap). Associates the caption with the control:
// pass `htmlFor` explicitly, or — when children is a single element without an
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
        <span className="req" title="required" aria-hidden="true">
          {" "}
          *
        </span>
      )}
    </>
  );
  return (
    <div className="field">
      {controlId ? (
        <label className="field-label" htmlFor={controlId}>
          {caption}
        </label>
      ) : (
        <span className="field-label">{caption}</span>
      )}
      {content}
      {hint != null && <span className="field-hint">{hint}</span>}
    </div>
  );
}

export const TextInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function TextInput({ className, ...props }, ref) {
    return <input ref={ref} className={cx("field-control", className)} {...props} />;
  },
);

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, ...props }, ref) {
    return (
      <span className="field-select-wrap">
        <select ref={ref} className={cx("field-control", className)} {...props} />
      </span>
    );
  },
);

export const TextArea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function TextArea({ className, ...props }, ref) {
  return <textarea ref={ref} className={cx("field-control", className)} {...props} />;
});
