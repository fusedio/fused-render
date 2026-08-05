// The file explorer's homepage (/explorer): a minimal launcher — the bookmark
// tree as a page, plus the workspace root as an always-present entry point so
// an empty-bookmarks user can still get into the tree. Entering any target
// navigates into /explorer/view/... (the explorer proper).
import { navigate } from "@platform/lib/router";
import { FolderIcon } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import BookmarksSection from "@apps/explorer/sidebar/BookmarksSection";

export default function FilesHome({ config }: { config: Config }) {
  return (
    <div className="files-home">
      <div className="files-home-inner">
        <h1 className="files-home-title">File Explorer</h1>
        <div className="sidebar-section">
          <a
            href="#"
            className="sidebar-item"
            onClick={(e) => {
              e.preventDefault();
              if (config.fused_dir) navigate(config.fused_dir, { isDir: true });
            }}
          >
            <span className="icon">
              <FolderIcon />
            </span>{" "}
            Fused
          </a>
        </div>
        <BookmarksSection />
      </div>
    </div>
  );
}
