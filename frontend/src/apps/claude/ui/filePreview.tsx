// ONE opener for the whole chat. The provider sits at the ClaudeChat root and
// renders at most one `FilePreviewModal`; anything below (tool chips, reply
// markdown, the artifact strip, the Files list) calls `useFilePreview()`.
//
// NO PROVIDER = `null`, deliberately: ArtifactRow also renders on the landing
// page outside any chat, and falls back to its old behaviour there.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import "../styles/filepreview.css";
import { resolveAgainst } from "../protocol/filePaths";
import { FilePreviewModal } from "./FilePreviewModal";

export type OpenPreview = (path: string) => void;

const FilePreviewContext = createContext<OpenPreview | null>(null);

export function useFilePreview(): OpenPreview | null {
  return useContext(FilePreviewContext);
}

export { FilePreviewContext };

export function FilePreviewProvider({
  cwd,
  children,
}: {
  cwd?: string | null;
  children: ReactNode;
}) {
  const [path, setPath] = useState<string | null>(null);
  const open = useCallback<OpenPreview>(
    (p) => {
      if (p) setPath(resolveAgainst(p, cwd));
    },
    [cwd],
  );
  const close = useCallback(() => setPath(null), []);
  const value = useMemo(() => open, [open]);
  return (
    <FilePreviewContext.Provider value={value}>
      {children}
      {path ? <FilePreviewModal path={path} onClose={close} /> : null}
    </FilePreviewContext.Provider>
  );
}
