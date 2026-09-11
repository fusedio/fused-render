import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { createMemoryParamsStore } = await import("./store");
const { useChatParam, useChatParams } = await import("./useChatParams");

test("useChatParam / useChatParams follow the store", () => {
  const store = createMemoryParamsStore({ session_id: "a" });
  function Probe() {
    const sid = useChatParam(store, "session_id");
    const all = useChatParams(store);
    return <div>{`${sid ?? "-"}:${Object.keys(all).length}`}</div>;
  }
  let r: ReturnType<typeof create> | undefined;
  act(() => {
    r = create(<Probe />);
  });
  expect(r!.toJSON()).toMatchObject({ children: ["a:1"] });
  act(() => {
    store.set({ session_id: "b", run: "r" });
  });
  expect(r!.toJSON()).toMatchObject({ children: ["b:2"] });
});
