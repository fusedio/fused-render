// A file opened as an OVERLAY over the chat, so reading an artifact or a file
// Claude touched never switches the screen the reader is on.
//
// The body is the same same-origin `/render` iframe the app uses for previews
// (`paneSrcFor` with the `_render` sentinel), so `/render` picks the right
// template for the file type. Portals to the TOP document like PlanModal.
// "Open in explorer" is the one door that navigates, and the reader chose it.
import { useEffect, useState } from "react";

import { statPath } from "@platform/lib/api";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";

import { paneSrcFor } from "../pane/paneUrl";
import { topDocumentBody } from "./topContainer";

export interface FilePreviewModalProps {
  path: string;
  onClose: () => void;
  /** Injectable so a suite needs no module mock. */
  stat?: (path: string) => Promise<unknown>;
}

type Exists = "checking" | "yes" | "no";

export function FilePreviewModal({ path, onClose, stat = statPath }: FilePreviewModalProps) {
  const [exists, setExists] = useState<Exists>("checking");
  useEffect(() => {
    let live = true;
    setExists("checking");
    stat(path).then(
      () => live && setExists("yes"),
      () => live && setExists("no"),
    );
    return () => {
      live = false;
    };
  }, [path, stat]);

  const name = path.replace(/\/+$/, "").split("/").pop() || path;
  const src = paneSrcFor({ mode: "_render", path: "" }, path);
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
            onClick={() => window.open(src, "_blank", "noopener")}
          >
            Open in new tab
          </button>
        </>
      }
    >
      <div className="chat-root c-filepreview-root">
        {exists === "no" ? (
          <div className="c-fp-missing">File no longer exists</div>
        ) : exists === "yes" ? (
          <iframe className="c-fp-frame" src={src} title={"Preview of " + name} />
        ) : null}
      </div>
    </Modal>
  );
}

export default FilePreviewModal;
