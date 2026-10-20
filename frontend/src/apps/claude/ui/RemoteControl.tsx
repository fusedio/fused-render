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

const POLL_MS = { off: 15000, starting: 1000, active: 4000 } as const;

export function useRemoteControl(sessionId: string, file: string) {
  const [view, setView] = useState<RemoteControlView>({ state: "off" });
  const [asking, setAsking] = useState<{ name: string } | null>(null);
  const gen = useRef(0);

  const apply = useCallback((r: RemoteControlReply) => {
    if ("error" in r) {
      notify({ title: r.error, tone: "info" });
      return;
    }
    if (r.state === "ended") {
      const failed = r.exit_code !== 0 && r.exit_code !== null;
      notify({
        title: failed ? "Remote Control stopped with an error" : "Remote Control ended",
        ...(failed && r.tail ? { detail: r.tail } : {}),
        tone: failed ? "error" : "info",
      });
      setView({ state: "off" });
    } else if (r.state === "active") setView({ state: "active", url: r.url });
    else if (r.state === "starting") setView({ state: "starting" });
    else setView({ state: "off" });
  }, []);

  const refresh = useCallback(() => {
    if (!sessionId) return;
    const g = gen.current;
    remoteControlStatus(sessionId).then((r) => { if (g === gen.current) apply(r); }, () => {});
  }, [sessionId, apply]);

  useEffect(() => {
    gen.current += 1;
    setView({ state: "off" });
    refresh();
    return () => { gen.current += 1; };
  }, [refresh]);

  const polling = view.state;
  useEffect(() => {
    if (!sessionId) return;
    const tick = pauseWhileHidden(refresh);
    const id = window.setInterval(tick, POLL_MS[polling]);
    return () => { window.clearInterval(id); tick.cancel(); };
  }, [sessionId, polling, refresh]);

  const act = useCallback((call: () => Promise<RemoteControlReply>, failure: string) => {
    const g = ++gen.current;
    call().then(
      (r) => {
        if (g !== gen.current) return;
        gen.current += 1;
        apply(r);
      },
      (e: unknown) => {
        if (g !== gen.current) return;
        gen.current += 1;
        notify({ title: e instanceof Error ? e.message : failure, tone: "error" });
      },
    );
  }, [apply]);

  const ask = useCallback((name: string) => setAsking({ name }), []);
  const cancel = useCallback(() => setAsking(null), []);

  const start = useCallback(() => {
    const name = asking?.name ?? "";
    setAsking(null);
    act(() => startRemoteControl(file, sessionId, name || undefined), "Remote Control did not start");
  }, [act, asking, file, sessionId]);

  const stop = useCallback(
    () => act(() => stopRemoteControl(sessionId), "Remote Control did not stop"),
    [act, sessionId],
  );

  return { view, asking: asking !== null, ask, start, cancel, stop };
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
