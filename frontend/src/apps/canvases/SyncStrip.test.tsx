// SyncStrip's "Publish now": offered while the publish waits on a Claude
// session's turn (`waiting_on_session`), never otherwise; a press publishes.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { SyncStrip } = await import("./SyncStrip");
const { emptySyncState } = await import("./sync-state");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function strip(waiting: boolean | undefined, busy = false, onPublish = () => {}) {
  const server = emptySyncState("alpha");
  server.watching = true;
  server.legs.disk_to_server = {
    ...server.legs.disk_to_server,
    state: "pending",
    oldest_age_ms: 1000,
    reason: waiting ? "waiting for another Claude session in this folder to finish its turn" : null,
    waiting_on_session: waiting,
  };
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      <SyncStrip
        env="unstable"
        name="alpha"
        server={server}
        queue={[]}
        failure={null}
        owned={null}
        live
        pull={{ busy: false, disabled: false, error: null, onPull: () => {} }}
        publish={{ busy, error: null, onPublish }}
        open={false}
        onToggle={() => {}}
        onBack={() => {}}
        workbenchUrl={null}
      />,
    );
  });
  mounted.push(r);
  return r;
}

const publishButton = (r: ReactTestRenderer): ReactTestInstance | undefined =>
  r.root.findAll(
    (n) =>
      n.type === "button" &&
      ["Publish now", "Publishing…"].includes(
        ([] as unknown[]).concat(n.props.children).filter((c) => typeof c === "string").join(""),
      ),
  )[0];

test("offers Publish now while the publish waits on a session, and a press publishes", () => {
  let pressed = 0;
  const r = strip(true, false, () => {
    pressed += 1;
  });
  const button = publishButton(r);
  expect(button).toBeDefined();
  act(() => button!.props.onClick());
  expect(pressed).toBe(1);
});

test("no Publish now for a publish that waits on nothing a user can skip", () => {
  expect(publishButton(strip(false))).toBeUndefined();
  expect(publishButton(strip(undefined))).toBeUndefined(); // an older server
});

test("keeps the button, disabled, while its publish runs", () => {
  const button = publishButton(strip(false, true));
  expect(button).toBeDefined();
  expect(button!.props.disabled).toBe(true);
});
