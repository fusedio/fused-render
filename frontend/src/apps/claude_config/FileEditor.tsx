// A plain text editor for one config file: loads once, edits in a monospace
// textarea, saves on demand. Skills, memory files and the other harnesses'
// instruction/settings files all open in it.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { toastErr, toastOk } from "./bits";

export function FileEditor({
  load,
  save,
  label,
  rows = 16,
  extra,
}: {
  load: () => Promise<string>;
  /** Resolves null on success, else the error to show. */
  save: (text: string) => Promise<string | null>;
  label: string;
  rows?: number;
  /** Beside Save: a delete button, a path caption. */
  extra?: ReactNode;
}) {
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    try {
      const err = await save(text);
      if (err) {
        toastErr(err);
        return;
      }
      setSaved(text);
      toastOk("Saved");
    } catch (e) {
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
        rows={rows}
        spellCheck={false}
        className="cc-fileeditor-text"
        onChange={(e) => setText(e.target.value)}
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
        {extra}
      </div>
    </div>
  );
}
