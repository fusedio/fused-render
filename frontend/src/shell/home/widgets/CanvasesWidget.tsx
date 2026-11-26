// Your canvases in the default workbench environment, newest update first.
// A row opens the canvas editor, which starts its sync (and clones it) itself.
import { timeAgo } from "@platform/lib/format";
import { useCanvasesFeature } from "@apps/canvases/feature-flag";
import { canvasWorkspacePath, catalogPath, type CanvasEntry, type WorkbenchEnv } from "@apps/canvases/api";
import { CanvasGlyph } from "@apps/canvases/glyph";
import { useHomeCanvases } from "../data";
import { softNavigate } from "../strip";
import { itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ItemIcons, ItemList, ListSkeleton, type WidgetItem } from "./bits";

export const CANVASES_SEE_ALL = "/canvases";

export function canvasItem(env: WorkbenchEnv, c: CanvasEntry): WidgetItem {
  const udfs = c.n_code_udfs ?? c.n_udfs;
  const when = timeAgo(c.updated_at ?? c.mtime);
  const sub = [udfs != null ? `${udfs} UDF${udfs === 1 ? "" : "s"}` : "", when ? `updated ${when}` : ""].filter(Boolean).join(" · ");
  return {
    key: c.id ?? c.name,
    name: c.name.replace(/_/g, " "),
    sub: sub || undefined,
    icon: c.preview_url ? <img src={c.preview_url} alt="" className="hw-canvas-thumb" /> : <CanvasGlyph size={18} />,
    href: canvasWorkspacePath(env, c.name),
  };
}

export function CanvasesWidget({ widget }: { widget: Widget }) {
  const on = useCanvasesFeature();
  return (
    <div className="hw-body">
      {on ? <CanvasesBody widget={widget} /> : <EmptyLine>Turn on Fused Workbench in Preferences to see your canvases here.</EmptyLine>}
    </div>
  );
}

function CanvasesBody({ widget }: { widget: Widget }) {
  const { data, error, retry } = useHomeCanvases();
  if (error && !data) return <ErrorLine message={error} onRetry={retry} />;
  if (!data) return <ListSkeleton rows={3} label="Loading canvases" />;
  if (data.kind === "no-cli") return <EmptyLine>The Fused CLI isn't installed yet.</EmptyLine>;
  if (data.kind === "no-env") return <EmptyLine>No workbench environment is set up.</EmptyLine>;
  if (data.kind === "signed-out") {
    return (
      <EmptyLine>
        <a href={CANVASES_SEE_ALL} onClick={(e) => softNavigate(e, CANVASES_SEE_ALL)}>
          Sign in to Fused
        </a>{" "}
        to see your canvases.
      </EmptyLine>
    );
  }
  if (!data.canvases.length) return <EmptyLine>No canvases yet.</EmptyLine>;
  const items = data.canvases.map((c) => canvasItem(data.env, c));
  const cap = itemCapacity(widget.size, widget.format);
  const more = catalogPath(data.env);
  return widget.format === "icons" ? (
    <ItemIcons items={items} cap={cap} moreHref={more} />
  ) : (
    <ItemList items={items} cap={cap} moreHref={more} />
  );
}
