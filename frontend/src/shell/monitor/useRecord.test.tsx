// useRecord reads the recording status only while `enabled` (the System chip's
// popover is closed most of the time and must not fetch).
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import { useRecord, type RecordApi } from "@shell/monitor/useRecord";

test("no status read while disabled; one read when it turns on", async () => {
  let reads = 0;
  const api: RecordApi = {
    start: async () => ({ recording: false, seconds_left: 0, recordings: [] }),
    status: async () => {
      reads++;
      return { recording: false, seconds_left: 0, recordings: [] };
    },
  };
  function Probe({ enabled }: { enabled: boolean }) {
    useRecord(api, 1e9, enabled);
    return null;
  }
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<Probe enabled={false} />);
  });
  expect(reads).toBe(0);
  await act(async () => r.update(<Probe enabled />));
  expect(reads).toBe(1);
  act(() => r.unmount());
});

test("an initial status read that lands after start() does not overwrite the started state", async () => {
  let resolveStatus!: (v: { recording: boolean; seconds_left: number; recordings: never[] }) => void;
  const api: RecordApi = {
    start: async () => ({ recording: true, seconds_left: 60, recordings: [] }),
    status: () => new Promise((res) => (resolveStatus = res)),
  };
  let out!: ReturnType<typeof useRecord>;
  function Probe() {
    out = useRecord(api, 1e9, true);
    return null;
  }
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<Probe />);
  });
  await act(async () => {
    await out.start();
  });
  expect(out.recording).toBe(true);
  await act(async () => resolveStatus({ recording: false, seconds_left: 0, recordings: [] }));
  expect(out.recording).toBe(true);
  act(() => r.unmount());
});
