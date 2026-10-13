import type { Bot } from "@apps/bots/lib/api";
import { Face } from "@apps/bots/components/Face";
import { useHomeBots } from "../data";
import { homeBots } from "../bots";
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
  // step/title survive a run, and a settled bot's note can be a stale running-phase one, so only running, waiting and error show detail.
  // A run start resets step and note but not title: a step-0 running bot shows its note (e.g. a model download), not the last run's page.
  const step = b.step && b.step_cap ? `step ${b.step}/${b.step_cap}` : "";
  const detail = b.status === "running" ? (step ? `${step} · ${b.title || b.note || ""}`.replace(/ · $/, "") : b.note)
    : b.status === "waiting" || b.status === "error" ? b.note : "";
  const sub = detail || STATUS_PHRASE[b.status] || undefined;
  return {
    key: b.id,
    name: b.name,
    icon: <Face b={b} />,
    sub,
    href: `${BOTS_HREF}?bot=${encodeURIComponent(b.id)}`,
    pill: { label: b.status, tone: TONE[b.status] ?? "idle" },
  };
}

export function BotsWidget({ widget }: { widget: Widget }) {
  const { data, error, retry } = useHomeBots();
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
  const shown = homeBots(data);
  if (!shown.length) return <div className="hw-body"><EmptyLine>All bots are hidden.</EmptyLine></div>;
  return (
    <div className="hw-body">
      <ItemList items={shown.map(toItem)}cap={itemCapacity(widget.size, "list")} moreHref={BOTS_HREF} variant="tall" />
    </div>
  );
}
