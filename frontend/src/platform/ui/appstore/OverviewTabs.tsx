// The body's left column on a detail page: Overview (the readme) · Files (the
// file table) behind underline tabs — the Marketplace app page and the
// `.fused` install page both show exactly this, so it is one block.
//
// `readme` is the raw markdown: `undefined` while it is still loading (a
// line skeleton), `null`/empty when the app has none (the `emptyText` line).
// The leading `# Title` and the leading screenshot line are trimmed first
// (`trimReadme`), so the Overview starts with prose instead of repeating the
// hero and the preview stage right above it.
import { useMemo, useState } from "react";
import { FilesTable, type FileRow } from "./FilesTable";
import { MarkdownView, type UrlResolver } from "./MarkdownView";
import { SegmentedTabs } from "./DetailLayout";
import { Skel } from "./Skeletons";

/** Drop a leading `# Title` and a leading image line that is the preview. */
export function trimReadme(md: string): string {
  let lines = md.replace(/\r/g, "").split("\n");
  const skipBlank = () => {
    while (lines.length && !lines[0].trim()) lines = lines.slice(1);
  };
  skipBlank();
  if (lines.length && /^#\s+/.test(lines[0])) lines = lines.slice(1);
  skipBlank();
  if (lines.length && /^!\[[^\]]*\]\((\.\/)?preview\.(png|jpe?g|webp|gif)(\s+"[^"]*")?\)\s*$/i.test(lines[0])) {
    lines = lines.slice(1);
  }
  skipBlank();
  return lines.join("\n");
}

/** The first prose paragraph of a readme as plain text (after `trimReadme`),
 *  clipped to `max` characters — a one-liner for a hero that has no
 *  description of its own. Null when the readme opens with no paragraph. */
export function readmeLead(md: string, max = 180): string | null {
  const body = trimReadme(md);
  for (const block of body.split(/\n\s*\n/)) {
    const t = block.trim();
    if (!t || /^(#|```|>|\||[-*+]\s|\d+\.\s|!\[|<)/.test(t)) continue;
    const plain = t
      .replace(/\s+/g, " ")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/[*_`~]+/g, "")
      .trim();
    if (!plain) continue;
    if (plain.length <= max) return plain;
    const cut = plain.slice(0, max);
    return cut.slice(0, Math.max(cut.lastIndexOf(" "), max - 20)).replace(/[,;:.\s]+$/, "") + "…";
  }
  return null;
}

/** A readme-shaped shimmer (the Overview while the readme loads). */
export function ReadmeSkeleton() {
  return (
    <div className="as-readme-skel" aria-hidden="true">
      {[92, 84, 96, 60, 0, 88, 76].map((w, k) =>
        w ? <Skel key={k} w={`${w}%`} h={12} r={4} /> : <span key={k} style={{ height: 10 }} />,
      )}
    </div>
  );
}

export interface OverviewTabsProps {
  /** Raw markdown; `undefined` = still loading, null/"" = no readme. */
  readme: string | null | undefined;
  baseUrl?: string;
  resolveUrl?: UrlResolver;
  /** `undefined` = still loading (the tab shows the count from `fileCount`). */
  files: FileRow[] | undefined;
  /** The Files tab's count before `files` lands. */
  fileCount?: number;
  emptyText?: string;
  label?: string;
}

export function OverviewTabs({
  readme,
  baseUrl,
  resolveUrl,
  files,
  fileCount,
  emptyText = "This app has no readme yet.",
  label = "App details",
}: OverviewTabsProps) {
  const [tab, setTab] = useState("overview");
  const trimmed = useMemo(() => (readme ? trimReadme(readme) : ""), [readme]);
  return (
    <>
      <SegmentedTabs
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "files", label: "Files", count: files?.length ?? fileCount },
        ]}
        value={tab}
        onChange={setTab}
        label={label}
      />
      {tab === "overview" ? (
        readme === undefined ? (
          <ReadmeSkeleton />
        ) : trimmed.trim() ? (
          <MarkdownView source={trimmed} baseUrl={baseUrl} resolveUrl={resolveUrl} />
        ) : (
          <p className="as-muted">{emptyText}</p>
        )
      ) : files ? (
        <FilesTable files={files} />
      ) : null}
    </>
  );
}

export default OverviewTabs;
