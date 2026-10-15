// A file opened as an OVERLAY over the chat, so reading an artifact or a file
// Claude touched never switches the screen the reader is on.
//
// The body frames the file's DEFAULT template: the first offerable stat entry
// (`paneOfferable`, the shell's own defaultTemplate rule, same as
// `previewSrcFor`), interactive and stamped `_noopen=1` so it is not recorded as
// an app open. A bare `/render` (the `_render` sentinel) is only the fallback
// for a folder, a file with no offerable template, or a stat without templates.
// Portals to the TOP document like PlanModal.
// "Open in explorer" is the one door that navigates, and the reader chose it.
import { useEffect, useState } from "react";

import { statPath, type StatResult } from "@platform/lib/api";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";

import { paneOfferable, paneSrcFor } from "../pane/paneUrl";
import { topDocumentBody } from "./topContainer";

export interface FilePreviewModalProps {
  path: string;
  onClose: () => void;
  /** Injectable so a suite needs no module mock. */
  stat?: (path: string) => Promise<Partial<StatResult> | unknown>;
}

type View = { kind: "checking" } | { kind: "missing" } | { kind: "ready"; src: string; tabSrc: string };

function viewFor(path: string, st: Partial<StatResult> | null | undefined): View {
  const entry = st && !st.is_dir ? paneOfferable(st.templates)[0] : undefined;
  if (entry) {
    return {
      kind: "ready",
      src: paneSrcFor(entry, path, { noOpen: true }),
      tabSrc: paneSrcFor(entry, path),
    };
  }
  const bare = paneSrcFor({ mode: "_render", path: "" }, path);
  return { kind: "ready", src: bare, tabSrc: bare };
}

export function FilePreviewModal({ path, onClose, stat = statPath }: FilePreviewModalProps) {
  const [view, setView] = useState<View>({ kind: "checking" });
  useEffect(() => {
    let live = true;
    setView({ kind: "checking" });
    stat(path).then(
      (st) => live && setView(viewFor(path, st as Partial<StatResult> | null | undefined)),
      () => live && setView({ kind: "missing" }),
    );
    return () => {
      live = false;
    };
  }, [path, stat]);

  const name = path.replace(/\/+$/, "").split("/").pop() || path;
  const tabSrc = view.kind === "ready" ? view.tabSrc : paneSrcFor({ mode: "_render", path: "" }, path);
  return (
    <Modal
      title={
        <>
          {name} <span className="c-fp-path">{path}</span>
        </>
      }
      ariaLabel={"Preview of " + name}
      onClose={onClose}
      dialogClassName="c-filepreview-modal c-tokens"
      plainBody
      getContainer={topDocumentBody}
      headActions={
        <>
          <button
            type="button"
            className="c-fp-btn"
            onClick={() => {
              navigateUrl(urlForFsPath(path));
              onClose();
            }}
          >
            Open in explorer
          </button>
          <button
            type="button"
            className="c-fp-btn"
            onClick={() => window.open(tabSrc, "_blank", "noopener")}
          >
            Open in new tab
          </button>
        </>
      }
    >
      <div className="chat-root c-filepreview-root">
        {view.kind === "missing" ? (
          <div className="c-fp-missing">File no longer exists</div>
        ) : view.kind === "ready" ? (
          <iframe className="c-fp-frame" src={view.src} title={"Preview of " + name} />
        ) : null}
      </div>
    </Modal>
  );
}

export default FilePreviewModal;
