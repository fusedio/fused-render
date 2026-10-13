// The phone pages the local-network listener (fused_render/lan.py) serves: the
// app grid at `/`, the bot list at `/bots` and a bot's chat at `/bots/<id>`.
// Their own Vite entry (lan.html) so a phone downloads this and not the shell;
// shares the shell's tokens + Tailwind layer. The bot screens are a lazy chunk:
// the grid never pays for them.
import { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { LanApp } from "./App";
import { useRoute } from "./nav";
import "./lan.css";

const BotsList = lazy(() => import("./bots/screens").then((m) => ({ default: m.BotsList })));
const BotChat = lazy(() => import("./bots/screens").then((m) => ({ default: m.BotChat })));

function Root() {
  const route = useRoute();
  if (route.name === "grid") return <LanApp />;
  return (
    <Suspense fallback={null}>
      {route.name === "bots" ? <BotsList /> : <BotChat key={route.id} id={route.id} />}
    </Suspense>
  );
}

createRoot(document.getElementById("root")!).render(<Root />);
