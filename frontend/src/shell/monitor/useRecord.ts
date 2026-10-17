// "Record 60s": start a recording and follow it to the end. Shared by the
// Overview tab's button and the System chip's popover. `status` is read once
// at mount (a recording started in another window is picked up) and then once a
// second while one is running; the countdown is the server's `seconds_left`.
import { useCallback, useEffect, useRef, useState } from "react";
import { getRecordStatus, startRecord, type RecordStatus } from "./monitor-api";

export interface RecordApi {
  start: () => Promise<RecordStatus>;
  status: () => Promise<RecordStatus>;
}
const REAL: RecordApi = { start: startRecord, status: getRecordStatus };

export function useRecord(api: RecordApi = REAL, pollMs = 1_000) {
  const [status, setStatus] = useState<RecordStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const read = useCallback(async () => {
    try {
      const s = await api.status();
      if (live.current) setStatus(s);
    } catch (e) {
      if (live.current) setError((e as Error).message);
    }
  }, [api]);
  useEffect(() => {
    void read();
  }, [read]);
  const recording = !!status?.recording;
  useEffect(() => {
    if (!recording) return;
    const id = setInterval(() => void read(), pollMs);
    return () => clearInterval(id);
  }, [recording, read, pollMs]);
  const start = useCallback(async () => {
    setError(null);
    try {
      const s = await api.start();
      if (live.current) setStatus(s);
    } catch (e) {
      if (live.current) setError((e as Error).message || "Could not start recording");
    }
  }, [api]);
  const latest = status?.recordings?.[0] ?? null;
  return { status, recording, start, error, latest, dir: status?.dir ?? null };
}
