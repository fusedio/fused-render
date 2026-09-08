// The tray's own rules, the chip row that shows them, and the two gestures that
// fill it (inventory 03 §A/§C/§E). The pipeline is replaced wholesale — these
// are decisions this half of the app makes, and none of them needs a capture
// engine, a clipboard or a server.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { AttachTray } = await import("./AttachTray");
const { useAttachments } = await import("./useAttachments");
const { ComposerCard } = await import("./Composer");
const { DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_PERMISSION } = await import("./composer-defaults");
const { paneShotIn } = await import("../protocol/wire");
type Attachment = import("../shots/types").Attachment;
type Receipt = import("../shots/types").Receipt;
type AttachApi = import("./attachApi").AttachApi;
type Attachments = import("./useAttachments").Attachments;

let ids = 0;
function att(over: Partial<Attachment> = {}): Attachment {
  return { id: "a" + ++ids, kind: "image", view: "/shots/" + ids + ".png", ...over };
}

interface Spy {
  revoked: Attachment[];
  flashed: number;
}

function fakeApi(over: Partial<AttachApi> = {}): { api: AttachApi; spy: Spy } {
  const spy: Spy = { revoked: [], flashed: 0 };
  const api: AttachApi = {
    flash: () => {
      spy.flashed += 1;
      return () => {};
    },
    attachPane: async () => att({ kind: "pane", seat: "pane", thumb: "blob:pane" }),
    attachFiles: async function* (_dir, files) {
      for (const f of files) yield att({ kind: "file", name: f.name, view: "/shots/" + f.name });
    },
    attachPaths: (_dir, paths) =>
      paths.map((p) => att({ kind: "file", view: p, name: p, brought: true })),
    readDirs: (list) =>
      (list || []).map((s) => (s.view || "").replace(/\/[^/]*$/, "")).filter(Boolean),
    readDirsFor: (_dir, list) =>
      (list || []).map((s) => (s.view || "").replace(/\/[^/]*$/, "")).filter(Boolean),
    revoke: (a) => {
      if (a) spy.revoked.push(a);
    },
    toWire: (list) => (list || []).map((s) => ({ kind: s.kind, view: s.view })),
    receiptFor: (a): Receipt => ({ kind: a.kind, label: "attached", view: a.view }),
    probePruned: async () => false,
    dragHasAttachment: () => true,
    pathsFromDrop: () => [],
    filesFromPaste: () => [],
    ...over,
  };
  return { api, spy };
}

function mountTray(api: AttachApi, frame: HTMLIFrameElement | null = null) {
  let hook: Attachments | undefined;
  function Host() {
    hook = useAttachments({
      api,
      agentDir: "/t/claude",
      frame: () => frame,
      flashHost: () => null,
      paneNoun: "preview",
      shotsDir: "/shots",
    });
    return (
      <AttachTray
        items={hook.items}
        paneNoun="preview"
        onOpen={() => {}}
        onRemove={hook.remove}
      />
    );
  }
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(<Host />);
  });
  return {
    get: () => hook!,
    root: () => renderer!.root,
    /** Deliberately OUTSIDE `act` in the unmount test below, so React's paint
     *  cannot slip in ahead of the teardown. */
    unmount: () => renderer!.unmount(),
  };
}

test("chips land in the order the user brought them (T:11625)", async () => {
  const { api } = fakeApi();
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addFiles([
      { name: "one.csv" } as File,
      { name: "two.csv" } as File,
      { name: "three.csv" } as File,
    ]);
  });
  expect(tray.get().items.map((s) => s.name)).toEqual(["one.csv", "two.csv", "three.csv"]);
  // Three chips, and no pending seat left behind.
  expect(tray.get().items.every((s) => !s.pending)).toBe(true);
});

test("the pane seat is unique: a second capture REPLACES the first (D285, T:11309)", async () => {
  const { api, spy } = fakeApi();
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addFiles([{ name: "note.csv" } as File]);
  });
  await act(async () => {
    await tray.get().capture();
  });
  await act(async () => {
    await tray.get().capture();
  });
  const panes = tray.get().items.filter((s) => s.kind === "pane");
  expect(panes.length).toBe(1);
  // The replaced picture's blob URL is the only handle to it.
  expect(spy.revoked.length).toBe(1);
  expect(spy.flashed).toBe(2);
  // The file it stacked on top of keeps its place.
  expect(tray.get().items[0]!.name).toBe("note.csv");
});

test("the camera is inert while a capture is in flight (T:11203 shotBusy)", async () => {
  let release: ((a: Attachment) => void) | null = null;
  const { api } = fakeApi({
    attachPane: () =>
      new Promise<Attachment>((res) => {
        release = res;
      }),
  });
  const tray = mountTray(api);
  let first: Promise<void> | undefined;
  act(() => {
    first = tray.get().capture();
  });
  expect(tray.get().capturing).toBe(true);
  // A second press while the first is out is refused, and adds nothing.
  await act(async () => {
    await tray.get().capture();
  });
  expect(tray.get().items.length).toBe(0);
  await act(async () => {
    release!(att({ kind: "pane" }));
    await first;
  });
  expect(tray.get().capturing).toBe(false);
  expect(tray.get().items.length).toBe(1);
});

test("the chip's ✕ removes THIS one, revokes it, and leaves the others (T:10654)", async () => {
  const { api, spy } = fakeApi();
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addFiles([{ name: "a.csv" } as File, { name: "b.csv" } as File]);
  });
  const gone = tray.get().items[0]!;
  act(() => {
    tray.get().remove(gone);
  });
  expect(tray.get().items.map((s) => s.name)).toEqual(["b.csv"]);
  expect(spy.revoked).toContain(gone);
});

test("a send empties the tray into the wire, the read rules and the receipts", async () => {
  const { api } = fakeApi();
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addPaths(["/home/me/data/rows.csv"]);
  });
  let out: ReturnType<Attachments["take"]> | undefined;
  act(() => {
    out = tray.get().take();
  });
  expect(tray.get().items.length).toBe(0);
  expect(out!.blocks.length).toBe(1);
  expect(paneShotIn(out!.blocks[0]!)).toEqual([{ kind: "file", view: "/home/me/data/rows.csv" }]);
  // One Read rule per directory, and never the shots dir the spawn line already
  // allows (T:11698).
  expect(out!.readDirs).toEqual(["/home/me/data"]);
  expect(out!.receipts.length).toBe(1);
  expect(out!.items.length).toBe(1);
});

test("a send that never landed hands them back PREPENDED (T:16093, 16708)", async () => {
  const { api } = fakeApi();
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addFiles([{ name: "was-in-flight.csv" } as File]);
  });
  let out: ReturnType<Attachments["take"]> | undefined;
  act(() => {
    out = tray.get().take();
  });
  // A picture attached WHILE the send was in flight keeps its place after the
  // ones that were already waiting.
  await act(async () => {
    await tray.get().addFiles([{ name: "typed-after.csv" } as File]);
  });
  act(() => {
    tray.get().giveBack(out!.items);
  });
  expect(tray.get().items.map((s) => s.name)).toEqual(["was-in-flight.csv", "typed-after.csv"]);
});

test("an empty tray puts nothing on the wire", () => {
  const { api } = fakeApi();
  const tray = mountTray(api);
  let out: ReturnType<Attachments["take"]> | undefined;
  act(() => {
    out = tray.get().take();
  });
  expect(out).toEqual({ blocks: [], readDirs: [], receipts: [], items: [] });
});

// ---- the chips themselves --------------------------------------------------

function chipsOf(items: Attachment[]) {
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <AttachTray items={items} paneNoun="preview" onOpen={() => {}} onRemove={() => {}} />
    );
  });
  return renderer!.root;
}

test("a picture is a thumbnail button, a file is a glyph door, a refusal is neither", () => {
  const root = chipsOf([
    att({ kind: "pane", thumb: "blob:x" }),
    att({ kind: "file", name: "rows.csv", size: 2048 }),
    att({ kind: "image", view: null, why: "could not be saved" }),
  ]);
  // The picture: one <img> inside a button that opens the viewer.
  expect(root.findAllByProps({ className: "c-shotthumb" }).length).toBeGreaterThan(0);
  // The file: the glyph IS the button (T:7143 `isDoor`).
  const doors = root.findAll(
    (n) => n.type === "button" && n.props.className === "c-pinlbl c-shotdoc",
  );
  expect(doors.length).toBe(1);
  expect(doors[0]!.props.children).toBe("📄");
  // A refused attachment has nothing to open, so its glyph is a plain span —
  // one "failed" chip shape, whatever kind failed.
  const plain = root.findAll((n) => n.type === "span" && n.props.className === "c-pinlbl");
  expect(plain.length).toBe(1);
});

test("a file says how big it is and a refusal says why, on the row (T:7112, 7172)", () => {
  const root = chipsOf([
    att({ kind: "file", name: "rows.csv", size: 2048 }),
    att({ kind: "pane", view: null, why: "could not be saved" }),
  ]);
  const rows = root
    .findAll((n) => n.type === "span" && n.props.className === "c-txt")
    .map((n) => String(n.props.children));
  expect(rows[0]).toBe("rows.csv · 2 KB");
  expect(rows[1]).toBe("no pane screenshot — could not be saved");
});

test("a chip whose bytes are still on their way says so, and cannot be removed yet", () => {
  let removed = 0;
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <AttachTray
        items={[att({ kind: "file", view: null, pending: true })]}
        paneNoun="preview"
        onOpen={() => {}}
        onRemove={() => {
          removed += 1;
        }}
      />
    );
  });
  const x = renderer!.root.findAll((n) => n.type === "button" && !!n.props.disabled);
  expect(x.length).toBe(1);
  expect(removed).toBe(0);
});

test("an empty tray with no annotation chips draws no row at all", () => {
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <AttachTray items={[]} paneNoun="preview" onOpen={() => {}} onRemove={() => {}} />
    );
  });
  expect(renderer!.toJSON()).toBe(null);
});

// ---- paste and drop gating (T:11719-11790) --------------------------------

test("paste: only a clipboard carrying FILES is taken; words reach the box", () => {
  const { api } = fakeApi({
    filesFromPaste: (ev) => (ev.clipboardData ? [{ name: "shot.png" } as File] : []),
  });
  const taken: File[][] = [];
  const onPaste = (ev: { clipboardData?: DataTransfer | null; preventDefault(): void }) => {
    const picks = api.filesFromPaste(ev);
    if (!picks.length) return;
    ev.preventDefault();
    taken.push(picks);
  };
  let prevented = 0;
  onPaste({ clipboardData: null, preventDefault: () => (prevented += 1) });
  expect(taken.length).toBe(0);
  expect(prevented).toBe(0);
  onPaste({ clipboardData: {} as DataTransfer, preventDefault: () => (prevented += 1) });
  expect(taken.length).toBe(1);
  expect(prevented).toBe(1);
});

test("drop: real paths win over a copy of the same bytes (T:11777)", async () => {
  const { api } = fakeApi({
    pathsFromDrop: () => ["/home/me/x.png"],
  });
  const tray = mountTray(api);
  const dt = {
    types: ["Files", "application/x-fused-path"],
    files: [{ name: "x.png" } as File],
  } as unknown as DataTransfer;
  await act(async () => {
    const paths = api.pathsFromDrop(dt);
    if (paths.length) await tray.get().addPaths(paths);
    else await tray.get().addFiles(Array.from(dt.files));
  });
  // No upload happened: the path is the user's own file (`brought`).
  expect(tray.get().items.map((s) => s.view)).toEqual(["/home/me/x.png"]);
  expect(tray.get().items[0]!.brought).toBe(true);
});

// ---- the composer row re-prices itself when the seat changes (§G) ---------

const controls = {
  model: DEFAULT_MODEL,
  effort: DEFAULT_EFFORT,
  permission: DEFAULT_PERMISSION,
  setModel() {},
  setEffort() {},
  setPermission() {},
};

test("the camera seat and the chip row both bump the row's fit revision", () => {
  let renderer: ReactTestRenderer | undefined;
  const card = (over: Record<string, unknown>) => (
    <ComposerCard
      variant="chat"
      file="/p/app.py"
      sessionId=""
      controls={controls}
      status="idle"
      back="/explorer/view/p"
      onSend={() => {}}
      onStop={() => {}}
      {...over}
    />
  );
  act(() => {
    renderer = create(card({}));
  });
  const seats = () => renderer!.root.findByProps({ className: "c-composer-row" }).props.children;
  // Empty by default: the camera moved into the `#anncta` strip on 2026-08-27,
  // so nothing occupies the seat — but the seat exists.
  expect(seats().some((c: unknown) => c === undefined || c === null || c === false)).toBe(true);
  act(() => {
    renderer!.update(
      card({ camera: <button type="button" className="c-viewshot-pill" />, fitRevision: 2 }),
    );
  });
  expect(
    renderer!.root.findAll((n) => n.props.className === "c-viewshot-pill").length,
  ).toBe(1);
  // Attachments alone make the composer sendable, with no words at all (T:17903).
  act(() => {
    renderer!.update(card({ hasAttachments: true }));
  });
  const send = renderer!.root.findByProps({ className: "c-send" });
  expect(send.props.disabled).toBe(false);
});

test("the tray's own record is what a send and an unmount read — not the last render", async () => {
  // WHAT `live.current` IS FOR, and why a render is too late to write it.
  //
  // Every road into the tray writes after an await (`api` is the network), and
  // two things read the result without waiting for a paint: `take()`, which must
  // send what the tray holds at THAT moment, and the unmount cleanup, which is
  // the only thing left that can release these Blobs. Refreshed in the RENDER
  // body, the record missed everything written since the last paint — so a send
  // fired in that window went out WITHOUT the picture the user just added, and a
  // chat closed in it pinned that picture's Blob for the life of the page with
  // the chip that was its only other handle already gone.
  //
  // One `act` batch, no paint between the calls, which is that window exactly.
  const { api, spy } = fakeApi();
  const tray = mountTray(api);
  const back = att({ kind: "file", view: "/x/a.png", name: "a.png", brought: true });
  act(() => {
    // `giveBack` stands in for all four roads — they share one committer, and it
    // is the only one that needs no await to reach it.
    tray.get().giveBack([back]);
    // A send fired before the chip has painted carries it...
    expect(tray.get().take().items).toEqual([back]);
    // ...and so does the unmount revoke, which reads the same record.
    tray.get().giveBack([back]);
    tray.unmount();
  });
  expect(spy.revoked).toEqual([back]);
});

test("THE CAMERA TELLS THE PIPELINE WHETHER THE PANE IS OURS TO READ (Bugbot #1064)", async () => {
  // `xo` is the only thing that admits the tab share (T:9963), and the camera is
  // the only caller that knows which frame is being photographed. Asked for with
  // no options at all — as it was — a cross-origin pane the native path could
  // not shoot fell through to a DOM clone of a document this page cannot open.
  const seen: (boolean | undefined)[] = [];
  const { api } = fakeApi({
    attachPane: async (_dir, _frame, opts) => {
      seen.push(opts?.xo);
      return att({ kind: "pane", seat: "pane", thumb: "blob:pane" });
    },
  });
  const xoFrame = {
    get contentDocument(): Document {
      throw new Error("cross-origin");
    },
  } as unknown as HTMLIFrameElement;
  const xo = mountTray(api, xoFrame);
  await act(async () => {
    await xo.get().capture();
  });
  expect(seen).toEqual([true]);

  // And a pane on our own origin is NOT offered the tab share: it has a document
  // to clone, and a share prompt for a page we can read is a prompt for nothing.
  const ours = mountTray(api, { contentDocument: {} as Document } as HTMLIFrameElement);
  await act(async () => {
    await ours.get().capture();
  });
  expect(seen).toEqual([true, false]);
});

test("A PLACEHOLDER ALWAYS BECOMES A CHIP, even when the pipeline throws", async () => {
  // `attachFile` is written never to throw, and the tray must not depend on it:
  // a rejected iteration used to have the placeholder REMOVED, so the picture the
  // user dropped vanished with no chip, no error and nothing to retry from
  // (Bugbot, PR #1064). Every gesture gets an answer, even a refusal.
  const { api } = fakeApi({
    attachFiles: async function* (_dir, files) {
      yield att({ kind: "file", name: files[0]!.name, view: "/shots/one.csv" });
      throw new Error("disk full");
    },
  });
  const tray = mountTray(api);
  await act(async () => {
    await tray.get().addFiles([{ name: "one.csv" } as File, { name: "two.csv" } as File]);
  });
  const items = tray.get().items;
  expect(items.map((s) => s.name)).toEqual(["one.csv", "two.csv"]);
  // The one that landed is an ordinary attachment; the one that did not says so
  // ON THE ROW, and rides the message as a refusal exactly as a failed upload
  // does (T:11305).
  expect(items[0]!.view).toBe("/shots/one.csv");
  expect(items[1]!.view).toBeNull();
  expect(items[1]!.why).toBe("could not be saved");
  expect(items[1]!.viewNote).toBe("not attached: it could not be saved (disk full)");
  // And no placeholder is left claiming a file is still on its way.
  expect(items.every((s) => !s.pending)).toBe(true);
});
