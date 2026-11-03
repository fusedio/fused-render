// The custom environment's settings editor: ~/.fused/settings.toml in a
// modal, so a reader who picks "Custom" on the Canvases page with no settings
// file can paste their deployment's config right there and move on, rather
// than leaving for a terminal.
//
// The file is the editor's only state. Opening asks the server for its text
// (creating it empty when there is none, so the path shown is a real file);
// Save hands the text back and the server parses it before writing — a paste
// that is not TOML comes back as the parser's own sentence under the editor
// and the file on disk stays as it was. There is no CodeMirror in this
// bundle (the workbench's editor lives in its iframe), so the editor is a
// monospace textarea; config files are pasted, not authored, here.
import { useEffect, useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import { openFusedSettings, saveFusedSettings } from "./api";

const PLACEHOLDER = `# Your Fused deployment, e.g.
base_url = "https://<deployment>.fused.io/server/v1"

[auth]
credentials_path = "~/.fused/credentials-<deployment>"`;

export function FusedSettingsModal({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  /** The file was written and parses. `configured` says whether it names a
   *  `base_url` — the page re-reads status either way. */
  onSaved: (result: { configured: boolean; web_url: string }) => void;
}) {
  const [path, setPath] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const [focusSignal, setFocusSignal] = useState(0);

  useEffect(() => {
    let cancelled = false;
    openFusedSettings()
      .then((r) => {
        if (cancelled) return;
        setPath(r.path);
        setText(r.text);
        setLoaded(r.text);
        // The textarea exists only once the read lands; take focus then.
        setFocusSignal((n) => n + 1);
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async () => {
    if (busy || loaded === null) return;
    setBusy(true);
    setErr("");
    try {
      const r = await saveFusedSettings(text);
      if (!r.configured) {
        // Written, but it points nowhere: say so here, where the text is,
        // rather than closing onto the same prompt the reader came from.
        setLoaded(text);
        setErr(
          `Saved to ${r.path}, but it names no base_url — the custom environment ` +
            "still points at hosted Fused. Add one and save again.",
        );
        setBusy(false);
        return;
      }
      onSaved({ configured: r.configured, web_url: r.web_url });
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const dirty = loaded !== null && text !== loaded;

  return (
    <Modal
      title="Custom Fused environment"
      onClose={onClose}
      busy={busy}
      dirty={dirty}
      width={560}
      initialFocus={editorRef}
      focusSignal={focusSignal}
      dialogClassName="canvases-settings-modal"
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || loaded === null}
            onClick={() => void save()}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <p className="canvases-settings-lead">
        The custom environment follows the Fused CLI&rsquo;s own settings file. Paste the
        config for your deployment below — it needs at least a <code>base_url</code>.
      </p>
      <p className="canvases-settings-path">
        <code>{path ?? "~/.fused/settings.toml"}</code>
      </p>
      <textarea
        ref={editorRef}
        className="field-control canvases-settings-editor"
        value={text}
        placeholder={PLACEHOLDER}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        wrap="off"
        disabled={busy || loaded === null}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            void save();
          }
        }}
        aria-label="settings.toml"
      />
      {err && (
        <p className="canvases-settings-error" role="alert">
          {err}
        </p>
      )}
    </Modal>
  );
}
