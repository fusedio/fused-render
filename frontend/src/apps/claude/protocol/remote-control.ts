import { post } from "./agent";

const URL = "/api/claude/remote-control";

export type RemoteControlStatus =
  | { state: "off" }
  | { state: "starting"; url: string; terminal_id: string }
  | { state: "active"; url: string; terminal_id: string }
  | { state: "ended"; exit_code: number | null; tail: string };

export type RemoteControlReply = RemoteControlStatus | { error: string };

export function startRemoteControl(file: string, sessionId: string, name?: string): Promise<RemoteControlReply> {
  return post(URL, { action: "start", file, session_id: sessionId, ...(name ? { name } : {}) });
}

export function remoteControlStatus(sessionId: string): Promise<RemoteControlReply> {
  return post(URL, { action: "status", session_id: sessionId });
}

export function stopRemoteControl(sessionId: string): Promise<RemoteControlReply> {
  return post(URL, { action: "stop", session_id: sessionId });
}
