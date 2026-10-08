import type { Bot } from "@apps/bots/lib/api";
import { Face } from "@apps/bots/components/Face";
import { useBots } from "../data";
import { itemCapacity, type Widget } from "../layout";
import { BigCount, EmptyLine, ErrorLine, ItemList, ListSkeleton, type WidgetItem } from "./bits";

const BOTS_HREF = "/bots";

const TONE: Record<string, "ok" | "warn" | "err" | "idle"> = {
  running: "ok",
  waiting: "warn",
  error: "err",
  paused: "idle",
  idle: "idle",
};

const STATUS_PHRASE: Record<string, string> = {
  running: "Working",
  waiting: "Waiting on you",
  error: "Something went wrong",
  paused: "Paused",
  idle: "Idle",
};

export function toItem(b: Bot): WidgetItem {
  const step = b.step && b.step_cap ? `step ${b.step}/${b.step_cap}` : "";
  const what = b.title || b.note || "";
  return {
    key: b.id,
    name: b.name,
    icon: <Face b={b} />,
    sub: [step, what].filter(Boolean).join(" · ") || STATUS_PHRASE[b.status] || undefined,
    href: `${BOTS_HREF}?bot=${encodeURIComponent(b.id)}`,
    pill: { label: b.status, tone: TONE[b.status] ?? "idle" },
  };
}

export function BotsWidget({ widget }: { widget: Widget }) {
  const { data, error, retry } = useBots();
  if (error && !data) {
    return (
      <div className="hw-body">
        <ErrorLine message="Couldn't reach bots. Turn Bots on in Preferences if it's off." onRetry={retry} />
      </div>
    );
  }
  if (!data) return <div className="hw-body"><ListSkeleton rows={3} label="Loading bots" /></div>;
  if (!data.length) return <div className="hw-body"><EmptyLine>No bots yet.</EmptyLine></div>;
  if (widget.format === "count") {
    const running = data.filter((b) => b.status === "running").length;
    const waiting = data.filter((b) => b.status === "waiting").length;
    return (
      <div className="hw-body">
        <BigCount
          value={String(running)}
          caption={running === 1 ? "bot running" : "bots running"}
          accent={waiting ? `${waiting} waiting on you` : undefined}
        />
      </div>
    );
  }
  return (
    <div className="hw-body">
      <ItemList items={data.map(toItem)} cap={itemCapacity(widget.size, "list")} moreHref={BOTS_HREF} variant="tall" />
    </div>
  );
}
