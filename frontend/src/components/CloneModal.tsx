// Open a deployed app: paste its URL, see what would be downloaded, then clone it into
// ~/Documents/Fused as an ordinary local page (023 §8.3, viewer half; backend in
// fused_render/app_clone.py).
//
// Two steps on purpose, mirroring the GitHub deep-link confirm page: a preview
// (GET /api/clone-app/info — read-only, writes nothing) and then an explicit Clone. The
// preview exists so the user sees the file list, the download size, and the exact folder
// BEFORE anything lands on their disk — and so the folder it names is the folder the clone
// actually uses, not a guess that a collision could invalidate.
//
// Not the GitHub clone flow: there is no "update" branch here. An archive carries no
// provenance we can verify, so a second clone of the same page goes to a fresh folder
// rather than writing over what may be the user's edited copy.
import { useCallback, useEffect, useRef, useState } from "react";

import { cloneApp, cloneAppInfo, type ClonePreview, type CloneResult } from "../lib/api";
import { Field, TextInput } from "./field/fields";
import Modal from "./modal/Modal";

// Bytes → a short human string. Deliberately not a shared util import: the only other
// formatter in the app is inside the file-selection tree and keying this to it would
// couple two unrelated surfaces.
function humanBytes(n: number | null | undefined): string | null {
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export interface CloneModalProps {
  onClose: () => void;
  // Called with the cloned page's /view path so the caller can navigate to it — the modal
  // never navigates itself (the shell owns routing).
  onCloned: (result: CloneResult) => void;
  // Prefilled URL — the path bar passes the link the user pasted (§34 CL-1), and it is
  // auto-previewed below. The `fused-render://open?app=…` deep link that would also supply
  // it is DEFERRED (§34 CL-7), so no OS-level link routes here today.
  initialSrc?: string;
}

export default function CloneModal({ onClose, onCloned, initialSrc = "" }: CloneModalProps) {
  const [src, setSrc] = useState(initialSrc);
  const [preview, setPreview] = useState<ClonePreview | null>(null);
  const [busy, setBusy] = useState<null | "preview" | "clone">(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<CloneResult | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Supersession guard, the same discipline DeployModal's load uses: a slow preview must
  // not overwrite the state of a newer one (or of a clone already in flight).
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const trimmed = src.trim();
  // The preview is keyed to the URL it was fetched for: editing the field must invalidate
  // it, or Clone could act on a URL the user has since changed away from.
  const previewMatches = preview !== null && preview.url !== "" && trimmed !== "";

  const onPreview = useCallback(async () => {
    if (!trimmed || busy) return;
    setBusy("preview");
    setError(null);
    setPreview(null);
    try {
      const info = await cloneAppInfo(trimmed);
      if (!alive.current) return;
      setPreview(info);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(null);
    }
  }, [trimmed, busy]);

  const onClone = useCallback(async () => {
    if (!trimmed || busy) return;
    setBusy("clone");
    setError(null);
    try {
      // Pass the previewed folder through so the clone lands in the folder the confirm
      // button just named (CL-1) — the preview writes nothing, so it cannot reserve it.
      const result = await cloneApp(trimmed, preview?.folder);
      if (!alive.current) return;
      setDone(result);
      onCloned(result);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(null);
    }
  }, [trimmed, busy, onCloned, preview]);

  // A URL arrived with the modal, so preview it immediately — supplying one is already an
  // expression of intent. The clone itself still needs an explicit click; arriving with a
  // URL (or, once CL-7 lands, following a link) is not consent to write files.
  const autoPreviewed = useRef(false);
  useEffect(() => {
    if (initialSrc && !autoPreviewed.current) {
      autoPreviewed.current = true;
      void onPreview();
    }
  }, [initialSrc, onPreview]);

  const totalLabel = humanBytes(preview?.download_bytes) ?? humanBytes(preview?.bytes);

  return (
    <Modal
      title="Open a deployed app"
      onClose={onClose}
      initialFocus={inputRef}
      width={560}
      footer={
        done ? (
          <button type="button" className="btn btn-primary" onClick={onClose}>
            Done
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={onClose}
              disabled={busy === "clone"}
            >
              Cancel
            </button>
            {previewMatches ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={onClone}
                disabled={busy !== null}
              >
                {busy === "clone" ? "Cloning…" : `Clone to ${preview!.folder}`}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                onClick={onPreview}
                disabled={busy !== null || !trimmed}
              >
                {busy === "preview" ? "Checking…" : "Check link"}
              </button>
            )}
          </>
        )
      }
    >
      {done ? (
        <div className="deploy-files-body">
          <div>
            Cloned {done.files} file{done.files === 1 ? "" : "s"} to{" "}
            <strong>{done.folder}</strong>.
          </div>
          <div className="deploy-muted">{done.dest}</div>
        </div>
      ) : (
        <>
          <Field
            label="Page URL"
            hint="Paste the link to a deployed Fused Render page. Its publisher has to have allowed cloning."
          >
            <TextInput
              ref={inputRef}
              type="url"
              placeholder="https://…"
              value={src}
              disabled={busy !== null}
              onChange={(e) => {
                setSrc(e.target.value);
                // Any edit invalidates the preview — see previewMatches.
                setPreview(null);
                setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !busy) {
                  e.preventDefault();
                  void (previewMatches ? onClone() : onPreview());
                }
              }}
            />
          </Field>

          {preview && (
            <div className="deploy-files">
              <div className="deploy-files-head" style={{ cursor: "default" }}>
                <span className="deploy-files-title">{preview.name}</span>
                <span className="deploy-files-count">
                  {preview.files.length} file{preview.files.length === 1 ? "" : "s"}
                  {totalLabel ? ` · ${totalLabel}` : ""}
                </span>
              </div>
              <div className="deploy-files-body">
                <ul className="deploy-file-list">
                  {preview.files.map((f) => (
                    <li key={f.path} className="deploy-file">
                      <code title={f.path}>{f.path}</code>
                      <span className="deploy-file-tag">{humanBytes(f.bytes) ?? ""}</span>
                      <span className="deploy-file-action" />
                    </li>
                  ))}
                </ul>
                <div className="deploy-muted">
                  Will be cloned to <strong>{preview.folder}</strong> in your Fused folder.
                  {preview.renamed &&
                    " A folder named after the page already exists, so this one gets a" +
                      " numbered name — nothing you have is overwritten."}
                </div>
              </div>
            </div>
          )}

          {error && <div className="deploy-error">{error}</div>}
        </>
      )}
    </Modal>
  );
}
