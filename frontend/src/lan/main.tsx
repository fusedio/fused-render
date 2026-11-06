// The phone page the local-network listener (fused_render/lan.py) serves to a
// paired iPhone: the app grid at `/`, and under `/bots/*` and `/tasks/*` THE
// DESKTOP'S OWN PAGES (D1467) — `@apps/bots` (list, chat, dialogs, store) and
// the shell's Tasks page (the list, the peek with `@apps/claude`'s chat) —
// mounted here unchanged, under `html[data-host="lan"]` (lan.html), which is
// what `lan.css` keys the phone layout off and what `@platform/lib/host`'s
// `isLanHost()` reads to hide the few controls the LAN door 404s. One UI, one
// wire: a fix in the chat lands on the phone with no second copy to patch.
// Its own Vite entry (lan.html) so a phone downloads this page and not the
// shell; the bots and tasks pages are lazy chunks, the grid never pays for
// them. Boot mirrors the shell's main.tsx: config → flavor, the history
// patch that turns every pushState/replaceState into `fused:urlchange`
// (the bots app and the peek store write the URL that way), the ui-state
// hydrate the Tasks page's layout reads.
import { lazy, Suspense, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { getConfig } from "@platform/lib/api";
import { hydrateUiState } from "@platform/lib/hydrate-ui-state";
import { seedFlavor } from "@platform/lib/flavor";
import { LanApp } from "./App";
import { HeaderRow, peekUrl, useRoute } from "./nav";
import { Toaster } from "./ui/Toast";
import "./lan.css";

const Bots = lazy(() => import("@apps/bots").then((m) => ({ default: m.Bots })));
const Scheduled = lazy(() => import("@shell/Scheduled"));

const origReplaceState = history.replaceState.bind(history);
history.replaceState = function (...args: Parameters<History["replaceState"]>) {
  origReplaceState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};
const origPushState = history.pushState.bind(history);
history.pushState = function (...args: Parameters<History["pushState"]>) {
  origPushState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};

/** `/bots/<id>` is a chat one level in: the header's switch gives way to Back. */
function BotsSection({ id }: { id: string | null }) {
  useEffect(() => {
    document.title = "Bots";
  }, []);
  return (
    <div className={`lan-section lan-bots ${id ? "lan-bots--chat" : "lan-bots--list"}`}>
      {/* A chat carries its own header (ChatPane: Back, face, name); the switch is the list's. */}
      {id ? null : (
        <div className="lan-header">
          <HeaderRow
            active="bots"
            addLabel="New bot"
            onAdd={() => void import("@apps/bots/state/store").then((m) => m.openDialog({ kind: "newBot" }))}
          />
        </div>
      )}
      <div className="lan-host" id="content">
        <Suspense fallback={null}>
          <Bots />
        </Suspense>
      </div>
    </div>
  );
}

/** The shell's Tasks page, unscoped (it draws its own peek frame there). A
 *  `/tasks/<key>` deep link (the iOS shell, a widget) becomes the page's own
 *  `?peek=<key>` before it mounts, so the peek store adopts it on load. */
function TasksSection({ peek }: { peek: string | null }) {
  useEffect(() => {
    document.title = "Tasks";
  }, []);
  const rewrite = peek !== null && location.pathname !== "/tasks";
  useEffect(() => {
    if (rewrite) history.replaceState(history.state, "", peekUrl(peek));
  }, [rewrite, peek]);
  if (rewrite) return null;
  return (
    <div className={`lan-section lan-tasks ${peek ? "lan-tasks--peek" : "lan-tasks--list"}`}>
      <div className="lan-header">
        <HeaderRow active="tasks" />
      </div>
      <div className="lan-host" id="content">
        <Suspense fallback={null}>
          <Scheduled />
        </Suspense>
      </div>
    </div>
  );
}

/** The visual viewport's height (the part not under the keyboard) as CSS
 *  variables on <html>: --vvh and --vvtop. iOS shrinks only the VISUAL
 *  viewport for the keyboard (the `interactive-widget` meta is ignored there),
 *  so the page's frame, the task peek and the dialog sheets are sized by
 *  these (lan.css) and the composer rides on top of the keyboard. `data-kb`
 *  while the keyboard is up. */
function useVisualViewportVar() {
  useEffect(() => {
    const vv = window.visualViewport;
    const root = document.documentElement;
    const set = () => {
      const h = vv ? vv.height : window.innerHeight;
      root.style.setProperty("--vvh", `${Math.round(h)}px`);
      root.style.setProperty("--vvtop", `${Math.round(vv ? vv.offsetTop : 0)}px`);
      if (window.innerHeight - h > 120) root.setAttribute("data-kb", "");
      else root.removeAttribute("data-kb");
    };
    set();
    vv?.addEventListener("resize", set);
    vv?.addEventListener("scroll", set);
    window.addEventListener("resize", set);
    return () => {
      vv?.removeEventListener("resize", set);
      vv?.removeEventListener("scroll", set);
      window.removeEventListener("resize", set);
      root.style.removeProperty("--vvh");
      root.style.removeProperty("--vvtop");
      root.removeAttribute("data-kb");
    };
  }, []);
}

function Root() {
  const route = useRoute();
  useVisualViewportVar();
  if (route.name === "grid") return <LanApp />;
  return (
    <>
      {route.name === "bots" ? <BotsSection id={route.id} /> : <TasksSection peek={route.peek} />}
      <Toaster />
    </>
  );
}

const root = createRoot(document.getElementById("root")!);
Promise.all([getConfig({ signal: AbortSignal.timeout(20_000) }).catch(() => null), hydrateUiState().catch(() => undefined)]).then(
  ([config]) => {
    if (config) seedFlavor(config);
    root.render(<Root />);
  },
);
