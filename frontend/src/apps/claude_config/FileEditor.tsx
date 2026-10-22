// A plain text editor for one config file: loads once, edits in a monospace
// textarea that grows to its content (at least MIN_ROWS tall), saves on
// demand. Skills and memory files open in it.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { toastErr, toastOk } from "./bits";

const MIN_ROWS = 20;

export function FileEditor({
  load,
  save,
  label,
  extra,
}: {
  load: () => Promise<string>;
  /** Resolves null on success, else the error to show. */
  save: (text: string) => Promise<string | null>;
  label: string;
  /** Beside Save: a delete button, a path caption. */
  extra?: ReactNode;
}) {
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);

  // Grow to the content: reset to auto so a shrinking text shrinks the box.
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [text, saved]);

  useEffect(() => {
    let alive = true;
    setSaved(null);
    setError(null);
    load().then(
      (t) => {
        if (!alive) return;
        setSaved(t);
        setText(t);
      },
      (e) => alive && setError((e as Error).message),
    );
    return () => {
      alive = false;
    };
  }, [load]);

  const submit = useCallback(async () => {
    setBusy(true);
    setNote(null);
    try {
      const err = await save(text);
      if (err) {
        setNote({ ok: false, text: err });
        toastErr(err);
        return;
      }
      setSaved(text);
      setNote({ ok: true, text: "Saved" });
      toastOk("Saved");
    } catch (e) {
      setNote({ ok: false, text: (e as Error).message });
      toastErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [save, text]);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (saved === null) return <p className="cc-lrow-sub">Loading…</p>;
  return (
    <div className="cc-fileeditor">
      <Textarea
        aria-label={label}
        value={text}
        ref={area}
        rows={MIN_ROWS}
        spellCheck={false}
        className="cc-fileeditor-text"
        onChange={(e) => {
          setText(e.target.value);
          setNote(null);
        }}
      />
      <div className="cc-fileeditor-bar">
        <Button size="sm" disabled={busy || text === saved} onClick={() => void submit()}>
          Save
        </Button>
        {text !== saved && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setText(saved)}>
            Revert
          </Button>
        )}
        {note && (
          <span className={note.ok ? "cc-fileeditor-note" : "cc-fileeditor-note is-error"} role="status">
            {note.text}
          </span>
        )}
        {extra}
      </div>
    </div>
  );
}
