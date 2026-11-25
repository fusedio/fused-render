// The "Share with bot" sheet's host: mounted once in the shell (App.tsx), it
// renders whichever request `openShareBot` (platform/lib/share-bot) last made.
// The sheet itself is loaded on the first request.
import { lazy, Suspense } from "react";
import { closeShareBot, useShareBotRequest } from "@platform/lib/share-bot";

const ShareBotModal = lazy(() => import("./ShareBotModal"));

export function ShareBotHost() {
  const req = useShareBotRequest();
  if (!req) return null;
  return (
    <Suspense fallback={null}>
      <ShareBotModal key={req.seq} file={req.file} onClose={closeShareBot} />
    </Suspense>
  );
}
