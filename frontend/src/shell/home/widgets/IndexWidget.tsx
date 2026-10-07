import { useIndexStatus } from "@platform/lib/index-status";
import { timeAgo } from "@platform/lib/format";
import type { Widget } from "../layout";
import { BigCount, EmptyLine, ListSkeleton } from "./bits";

export function IndexWidget(_: { widget: Widget }) {
  const status = useIndexStatus(true);
  if (!status) return <div className="hw-body"><ListSkeleton rows={2} label="Loading index status" /></div>;
  if (!status.has_index) {
    return (
      <div className="hw-body">
        <EmptyLine>{status.scanning ? "Indexing your files…" : "No file index yet."}</EmptyLine>
      </div>
    );
  }
  const ago = timeAgo(status.last_completed_at);
  return (
    <div className="hw-body">
      <BigCount
        value={status.files_indexed.toLocaleString()}
        caption="files indexed"
        accent={status.scanning ? "updating now" : ago ? `updated ${ago}` : undefined}
      />
    </div>
  );
}
