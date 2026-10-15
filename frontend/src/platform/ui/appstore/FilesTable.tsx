// The "Files" tab of a detail page: every file the app ships, folder-sorted,
// with sizes and a total. Generic: `{path, size}` rows (the catalog's file
// list, or a `.fused` archive's manifest in phase 2).
//
// Long lists collapse past `initialRows` behind a "Show all N files" button,
// so a 400-file app does not turn the page into a scroll marathon.
import { useMemo, useState } from "react";
import { File, FileCode2, FileImage, FileJson, FileText } from "lucide-react";
import { formatSize } from "@platform/lib/format";

export interface FileRow {
  path: string;
  size: number;
}

export interface FilesTableProps {
  files: FileRow[];
  /** Rows shown before "Show all" (default 14). */
  initialRows?: number;
  /** Optional per-row link (e.g. the raw asset URL). */
  hrefFor?: (path: string) => string | null;
  className?: string;
}

function glyphFor(path: string) {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "ico"].includes(ext)) return FileImage;
  if (["json", "toml", "yaml", "yml", "lock"].includes(ext)) return FileJson;
  if (["md", "txt", "rst"].includes(ext)) return FileText;
  if (["py", "js", "ts", "tsx", "jsx", "html", "css", "sh", "mjs"].includes(ext)) return FileCode2;
  return File;
}

/** Root files first, then folders in name order — the order a file tree
 *  reads in, flattened. */
function treeOrder(a: FileRow, b: FileRow): number {
  const da = a.path.includes("/") ? 1 : 0;
  const db = b.path.includes("/") ? 1 : 0;
  return da - db || a.path.localeCompare(b.path);
}

export function FilesTable({ files, initialRows = 14, hrefFor, className }: FilesTableProps) {
  const [all, setAll] = useState(false);
  const sorted = useMemo(() => files.slice().sort(treeOrder), [files]);
  const total = useMemo(() => files.reduce((n, f) => n + (f.size || 0), 0), [files]);
  const shown = all ? sorted : sorted.slice(0, initialRows);
  return (
    <div className={"as-files" + (className ? " " + className : "")}>
      <table className="as-files-table">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col" className="is-num">
              Size
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((f) => {
            const Glyph = glyphFor(f.path);
            const slash = f.path.lastIndexOf("/");
            const dir = slash >= 0 ? f.path.slice(0, slash + 1) : "";
            const base = slash >= 0 ? f.path.slice(slash + 1) : f.path;
            const href = hrefFor?.(f.path) ?? null;
            const name = (
              <>
                {dir && <span className="as-files-dir">{dir}</span>}
                <span className="as-files-base">{base}</span>
              </>
            );
            return (
              <tr key={f.path}>
                <td>
                  <span className="as-files-name">
                    <Glyph size={14} strokeWidth={1.8} aria-hidden="true" className="as-files-glyph" />
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer">
                        {name}
                      </a>
                    ) : (
                      <span>{name}</span>
                    )}
                  </span>
                </td>
                <td className="is-num">{formatSize(f.size)}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>
              {files.length} {files.length === 1 ? "file" : "files"}
            </td>
            <td className="is-num">{formatSize(total)}</td>
          </tr>
        </tfoot>
      </table>
      {!all && sorted.length > initialRows && (
        <button type="button" className="as-link-btn as-files-more" onClick={() => setAll(true)}>
          Show all {sorted.length} files
        </button>
      )}
    </div>
  );
}

export default FilesTable;
