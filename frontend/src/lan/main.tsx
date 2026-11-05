// The phone pages the local-network listener (fused_render/lan.py) serves: the
// app grid at `/`, the bot list at `/bots` and a bot's chat at `/bots/<id>`,
// the task list at `/tasks` and one task at `/tasks/<key>`. Their own Vite
// entry (lan.html) so a phone downloads this and not the shell; shares the
// shell's tokens + Tailwind layer. The bot and task screens are lazy chunks:
// the grid never pays for them.
import { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { LanApp } from "./App";
import { useRoute } from "./nav";
import "./lan.css";

const BotsList = lazy(() => import("./bots/screens").then((m) => ({ default: m.BotsList })));
const BotChat = lazy(() => import("./bots/screens").then((m) => ({ default: m.BotChat })));
const TasksList = lazy(() => import("./tasks/screens").then((m) => ({ default: m.TasksList })));
const TaskView = lazy(() => import("./tasks/screens").then((m) => ({ default: m.TaskView })));

function Root() {
  const route = useRoute();
  if (route.name === "grid") return <LanApp />;
  return (
    <Suspense fallback={null}>
      {route.name === "bots" ? (
        <BotsList />
      ) : route.name === "bot" ? (
        <BotChat key={route.id} id={route.id} />
      ) : route.name === "tasks" ? (
        <TasksList />
      ) : (
        <TaskView key={route.key} taskKey={route.key} />
      )}
    </Suspense>
  );
}

createRoot(document.getElementById("root")!).render(<Root />);
