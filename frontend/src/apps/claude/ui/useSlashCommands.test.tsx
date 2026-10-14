import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useSlashCommands } = await import("./useSlashCommands");
import type { CommandRow } from "../protocol/types";

type Pending = { file: string; resolve(rows: CommandRow[]): void; reject(): void };

test("a new file drops the old list at once; a refetch keeps it; a failure clears it", async () => {
  const calls: Pending[] = [];
  const fetcher = (file: string) =>
    new Promise<{ commands: CommandRow[] }>((res, rej) => {
      calls.push({ file, resolve: (rows) => res({ commands: rows }), reject: () => rej(new Error("x")) });
    });
  let seen: { commands: CommandRow[] | null; refetch(): void } | null = null;
  function Probe({ file }: { file: string | null }) {
    seen = useSlashCommands(file, fetcher);
    return null;
  }
  const row = (name: string): CommandRow => ({ name, description: "", source: "x" });
  let r: ReactTestRenderer | undefined;
  await act(async () => {
    r = create(<Probe file="/a" />);
  });
  await act(async () => calls[0]!.resolve([row("a-cmd")]));
  expect(seen!.commands).toEqual([row("a-cmd")]);

  // Same file, refetch: the list stays until the answer.
  await act(async () => seen!.refetch());
  expect(seen!.commands).toEqual([row("a-cmd")]);
  await act(async () => calls[1]!.reject());
  expect(seen!.commands).toBeNull();

  await act(async () => {
    seen!.refetch();
  });
  await act(async () => calls[2]!.resolve([row("a2")]));
  expect(seen!.commands).toEqual([row("a2")]);

  // Another folder: gone immediately, before its own answer.
  await act(async () => r!.update(<Probe file="/b" />));
  expect(seen!.commands).toBeNull();
  // The old folder's late answer would not land either (its effect is gone).
  await act(async () => calls[3]!.resolve([row("b-cmd")]));
  expect(seen!.commands).toEqual([row("b-cmd")]);
  act(() => r!.unmount());
});
