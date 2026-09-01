// The phone grid: the page the local-network listener (fused_render/lan.py)
// serves at its `/`. Its own Vite entry (lan.html) so a phone downloads this
// and not the shell; shares the shell's tokens + Tailwind layer.
import { createRoot } from "react-dom/client";
import { LanApp } from "./App";
import "./lan.css";

createRoot(document.getElementById("root")!).render(<LanApp />);
