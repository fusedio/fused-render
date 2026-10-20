import { useCallback, useEffect, useRef, useState } from "react";

import { copyToClipboard } from "@platform/lib/clipboard";
import { notify } from "@platform/lib/notifications";
import { pauseWhileHidden } from "@platform/lib/pause-while-hidden";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import {
  remoteControlStatus,
  startRemoteControl,
  stopRemoteControl,
  type RemoteControlReply,
} from "../protocol/remote-control";

export const REMOTE_CONTROL_BLOCKED = "Remote Control is driving this chat";

export type RemoteControlView = { state: "off" } | { state: "starting" } | { state: "active"; url: string };

export function useRemoteControl(sessionId: string, file: string) {
  const [view, setView] = useState<RemoteControlView>({ state: "off" });
  const [asking, setAsking] = useState<{ name: string } | null>(null);
  const live = useRef(sessionId);
  live.current = sessionId;

  const apply = useCallback((sid: string, r: RemoteControlReply): boolean => {
    if (live.current !== sid) return false;
    if ("error" in r) {
      notify({ title: r.error, tone: "info" });
      return false;
    }
    if (r.state === "ended") {
      const failed = r.exit_code !== 0 && r.exit_code !== null;
      notify({
        title: failed ? "Remote Control stopped with an error" : "Remote Control ended",
        ...(failed && r.tail ? { detail: r.tail } : {}),
        tone: failed ? "error" : "info",
      });
      setView({ state: "off" });
      return true;
    }
    if (r.state === "active") setView({ state: "active", url: r.url });
    else if (r.state === "starting") setView({ state: "starting" });
    else setView({ state: "off" });
    return true;
  }, []);

  useEffect(() => {
    setView({ state: "off" });
    if (!sessionId) return;
    let gone = false;
    remoteControlStatus(sessionId).then((r) => { if (!gone) apply(sessionId, r); }, () => {});
    return () => { gone = true; };
  }, [sessionId, apply]);

  const polling = view.state;
  useEffect(() => {
    if (!sessionId || polling === "off") return;
    let gone = false;
    const tick = pauseWhileHidden(() => {
      remoteControlStatus(sessionId).then((r) => { if (!gone) apply(sessionId, r); }, () => {});
    });
    const id = window.setInterval(tick, polling === "starting" ? 1000 : 4000);
    return () => { gone = true; window.clearInterval(id); tick.cancel(); };
  }, [sessionId, polling, apply]);

  const ask = useCallback((name: string) => setAsking({ name }), []);

  const start = useCallback(() => {
    const name = asking?.name ?? "";
    setAsking(null);
    const sid = sessionId;
    startRemoteControl(file, sid, name || undefined).then(
      (r) => { apply(sid, r); },
      (e: unknown) => notify({ title: e instanceof Error ? e.message : "Remote Control did not start", tone: "error" }),
    );
  }, [asking, file, sessionId, apply]);

  const stop = useCallback(() => {
    const sid = sessionId;
    stopRemoteControl(sid).then(() => {}, () => {}).finally(() => {
      if (live.current === sid) setView({ state: "off" });
    });
  }, [sessionId]);

  return { view, asking: asking !== null, ask, start, cancel: () => setAsking(null), stop };
}

export function RemoteControlConfirm({ open, onGo, onCancel }: { open: boolean; onGo(): void; onCancel(): void }) {
  return (
    <AlertDialog open={open} onOpenChange={(o) => { if (!o) onCancel(); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Turn on Remote Control?</AlertDialogTitle>
          <AlertDialogDescription>
            You can continue this chat at claude.ai/code or in the Claude app. While it is on, this chat is
            read-only here and permission prompts go to claude.ai instead of this page. Claude Code will trust
            this folder.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onGo}>Turn on</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function RemoteControlNote({ view, onStop }: { view: RemoteControlView; onStop(): void }) {
  if (view.state === "off") return null;
  const stop = (
    <button type="button" onClick={onStop}>
      Stop
    </button>
  );
  if (view.state === "starting") return <>Connecting Remote Control… {stop}</>;
  return (
    <>
      <a href={view.url} target="_blank" rel="noreferrer">Open on claude.ai</a>{" "}
      <button
        type="button"
        onClick={() => {
          void copyToClipboard(view.url).then((ok) => notify({ title: ok ? "Link copied" : "Could not copy the link", tone: "info" }));
        }}
      >
        Copy link
      </button>{" "}
      {stop}
    </>
  );
}
