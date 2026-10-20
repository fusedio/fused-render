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
import { post } from "../protocol/agent";

export const REMOTE_CONTROL_CONNECTING = "Connecting Remote Control…";

export type RemoteControlView = { state: "off" } | { state: "starting" } | { state: "active"; url: string };
type Reply =
  | RemoteControlView
  | { state: "ended"; exit_code: number | null; tail: string }
  | { sent: true }
  | { error: string };

const POLL_MS = { off: 15000, starting: 1000, active: 4000 } as const;

const call = (body: Record<string, string>) => post<Reply>("/api/claude/remote-control", body);

function viewOf(r: Reply): RemoteControlView | null {
  if ("error" in r) {
    notify({ title: r.error, tone: "error" });
    return null;
  }
  if (!("state" in r)) return null;
  if (r.state === "active") return { state: "active", url: r.url };
  if (r.state === "starting") return { state: "starting" };
  if (r.state === "ended") {
    const failed = r.exit_code !== 0 && r.exit_code !== null;
    notify({
      title: failed ? "Remote Control stopped with an error" : "Remote Control ended",
      ...(failed && r.tail ? { detail: r.tail } : {}),
      tone: failed ? "error" : "info",
    });
  }
  return { state: "off" };
}

export function useRemoteControl(sessionId: string, file: string) {
  const [view, setView] = useState<RemoteControlView>({ state: "off" });
  const [askingName, setAskingName] = useState<string | null>(null);
  const gen = useRef(0);

  const refresh = useCallback(() => {
    if (!sessionId) return;
    const g = gen.current;
    call({ action: "status", session_id: sessionId }).then((r) => {
      const next = g === gen.current ? viewOf(r) : null;
      if (next) setView(next);
    }, () => {});
  }, [sessionId]);

  useEffect(() => {
    gen.current += 1;
    setView({ state: "off" });
    refresh();
    return () => { gen.current += 1; };
  }, [refresh]);

  useEffect(() => {
    if (!sessionId) return;
    const tick = pauseWhileHidden(refresh);
    const id = window.setInterval(tick, POLL_MS[view.state]);
    return () => { window.clearInterval(id); tick.cancel(); };
  }, [sessionId, view.state, refresh]);

  const act = useCallback((body: Record<string, string>, failure: string) => {
    const g = ++gen.current;
    const settle = (read: () => RemoteControlView | null) => {
      if (g !== gen.current) return;
      gen.current += 1;
      const next = read();
      if (next) setView(next);
      else refresh();
    };
    call({ ...body, session_id: sessionId }).then(
      (r) => settle(() => viewOf(r)),
      (e: unknown) => settle(() => {
        notify({ title: e instanceof Error ? e.message : failure, tone: "error" });
        return null;
      }),
    );
  }, [sessionId, refresh]);

  const start = useCallback(() => {
    setAskingName(null);
    act({ action: "start", file, ...(askingName ? { name: askingName } : {}) }, "Remote Control did not start");
  }, [act, askingName, file]);
  const stop = useCallback(() => act({ action: "stop" }, "Remote Control did not stop"), [act]);
  const cancel = useCallback(() => setAskingName(null), []);
  const say = useCallback((text: string) => {
    call({ action: "say", session_id: sessionId, text }).then(viewOf, (e: unknown) =>
      notify({ title: e instanceof Error ? e.message : "Remote Control did not take the message", tone: "error" }),
    );
  }, [sessionId]);

  return { view, asking: askingName !== null, ask: setAskingName, start, cancel, stop, say };
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
  const stop = <button type="button" onClick={onStop}>Stop</button>;
  if (view.state === "starting") return <>Connecting Remote Control… {stop}</>;
  const copy = () =>
    void copyToClipboard(view.url).then((ok) => notify({ title: ok ? "Link copied" : "Could not copy the link", tone: "info" }));
  return (
    <>
      Remote Control is on ·{" "}
      <a href={view.url} target="_blank" rel="noreferrer">Open on claude.ai</a>{" "}
      <button type="button" onClick={copy}>Copy link</button>{" "}
      {stop}
    </>
  );
}
