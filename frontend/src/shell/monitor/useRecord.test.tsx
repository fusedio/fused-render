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
