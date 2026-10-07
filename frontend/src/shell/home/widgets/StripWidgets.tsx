// The three strips that existed before widgets — Playground, Claude Sessions,
// Recent files — as widget bodies. Cards format is a horizontally scrolling
// strip (CardStrip): the measured card count sizes the cards so the next one
// peeks in at the right edge, and every item fetched is rendered; list format
// shows rows.
import { basename } from "@platform/lib/format";
import { spaLinkProps } from "@platform/lib/router";
import { loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { FolderPreviewCard, RecentPreviewCard } from "@apps/explorer/BookmarkCards";
import { tabHref } from "@apps/ai_models/routes";
import { PLAYGROUND_GROUPS, PlaygroundPreviewCard } from "../PlaygroundCard";
import { CardStrip } from "../CardStrip";
import { SkeletonRow } from "../skeleton";
import { MAX_ROW, useStripCount } from "../strip";
import { useHomeSessions } from "../data";
import { dims, itemCapacity, type Widget } from "../layout";
import { EmptyLine, ItemList, ListSkeleton, type WidgetItem } from "./bits";

export function PlaygroundWidget({ widget }: { widget: Widget }) {
  const { rows } = dims(widget.size);
  const { ref, count } = useStripCount();
  if (widget.format === "list") {
    const items: WidgetItem[] = PLAYGROUND_GROUPS.map((g) => ({
      key: g.capability,
      name: g.label,
      sub: g.blurb,
      href: tabHref("playground", `?cap=${encodeURIComponent(g.capability)}`),
    }));
    return (
      <div ref={ref} className="hw-body">
        <ItemList items={items} cap={itemCapacity(widget.size, "list")} moreHref={tabHref("playground", "")} />
      </div>
    );
  }
  return (
    <div ref={ref} className="hw-body">
      <CardStrip count={count} rows={rows} total={PLAYGROUND_GROUPS.length}>
        {PLAYGROUND_GROUPS.map((group) => (
          <PlaygroundPreviewCard key={group.capability} group={group} />
        ))}
      </CardStrip>
    </div>
  );
}

export function SessionsWidget({ widget }: { widget: Widget }) {
  const { rows } = dims(widget.size);
  const cards = widget.format === "cards";
  const { ref, count, limit } = useStripCount();
  const cap = cards ? (count ?? 0) * rows : itemCapacity(widget.size, "list");
  const sessions = useHomeSessions(cards ? limit : Math.min(cap + 1, MAX_ROW), cards ? rows : 1);
  return (
    <div ref={ref} className="hw-body">
      {sessions === null ? (
        cards ? (
          <SkeletonRow count={cap} label="Loading Claude sessions" variant="folder" />
        ) : (
          <ListSkeleton rows={2} label="Loading Claude sessions" />
        )
      ) : !sessions.length ? (
        <EmptyLine>No Claude Code sessions found on this machine.</EmptyLine>
      ) : cards ? (
        <CardStrip count={count} rows={rows} total={sessions.length}>
          {sessions.map((f) => (
            <FolderPreviewCard key={f.path} path={f.path} />
          ))}
        </CardStrip>
      ) : (
        <ItemList
          items={sessions.map((f) => ({
            key: f.path,
            name: basename(f.path),
            sub: f.path,
            ...spaLinkProps(f.path, { isDir: true }),
          }))}
          cap={cap}
          moreHref="/explorer?tab=sessions"
        />
      )}
    </div>
  );
}

export function RecentsWidget({ widget }: { widget: Widget }) {
  useRecentsVersion();
  const { rows } = dims(widget.size);
  const cards = widget.format === "cards";
  const { ref, count } = useStripCount();
  const cap = cards ? (count ?? 0) * rows : itemCapacity(widget.size, "list");
  // Recents come from the same client cache the explorer home reads (raw MRU).
  const recents = loadRecents().entries.slice(0, MAX_ROW);
  return (
    <div ref={ref} className="hw-body">
      {!recents.length ? (
        <EmptyLine>Nothing opened yet. Files you view will show up here.</EmptyLine>
      ) : cards ? (
        <CardStrip count={count} rows={rows} total={recents.length}>
          {recents.map((r) => {
            const fsPath = recentFsPath(r.url);
            return (
              <RecentPreviewCard key={fsPath} url={r.url} path={fsPath} name={r.title || basename(fsPath)} />
            );
          })}
        </CardStrip>
      ) : (
        <ItemList
          items={recents.map((r) => {
            const fsPath = recentFsPath(r.url);
            return {
              key: fsPath,
              name: r.title || basename(fsPath),
              sub: fsPath,
              href: r.url,
            };
          })}
          cap={cap}
          moreHref="/explorer?tab=recents"
        />
      )}
    </div>
  );
}
