// Progress window for the macOS network installer (scripts/pkg/postinstall).
//
// Installer.app gives a package script no way to set its status text or move
// its progress bar: during the download it can only say "Running package
// scripts". So the postinstall (root) starts this, as the person who ran
// Installer, with `osascript -l JavaScript progress.js <status-file>`, and
// writes the download's progress into <status-file>; this only draws it.
//
// JavaScript for Automation, not a helper binary: osascript is on every Mac,
// a .js file is not Mach-O (nothing extra to sign or notarize), and the file
// is as reviewable as the postinstall itself.
//
// <status-file> holds one line, rewritten atomically by the postinstall:
//   <percent 0-100 or -1 for indeterminate>\t<message>
//   DONE | FAILED                      -> close the window and exit
// A missing file also means exit, so a postinstall that dies without writing a
// marker never leaves a window behind. Never blocks the install: the
// postinstall does not wait on this process.
ObjC.import('Cocoa');

function readStatus(path) {
  const s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  if (!s || s.isNil()) return null;
  return s.js.trim();
}

function run(argv) {
  const statusPath = argv[0];
  const title = argv[1] || 'Installer';
  if (!statusPath) return;

  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);  // no Dock icon

  const W = 420, H = 104;
  const win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
    $.NSMakeRect(0, 0, W, H), $.NSWindowStyleMaskTitled, $.NSBackingStoreBuffered, false);
  win.title = title;
  win.level = $.NSFloatingWindowLevel;  // above Installer's window
  win.releasedWhenClosed = false;

  const label = $.NSTextField.labelWithString('Preparing…');
  label.frame = $.NSMakeRect(20, 58, W - 40, 22);
  label.font = $.NSFont.systemFontOfSize(13);
  win.contentView.addSubview(label);

  const bar = $.NSProgressIndicator.alloc.initWithFrame($.NSMakeRect(20, 28, W - 40, 20));
  bar.style = $.NSProgressIndicatorStyleBar;
  bar.minValue = 0;
  bar.maxValue = 100;
  bar.indeterminate = true;
  bar.startAnimation(null);
  win.contentView.addSubview(bar);

  win.center;
  win.makeKeyAndOrderFront(null);
  app.activateIgnoringOtherApps(true);

  let last = '';
  for (;;) {
    // Pump events for up to 0.2 s so the window draws and the bar animates.
    for (;;) {
      const ev = app.nextEventMatchingMaskUntilDateInModeDequeue(
        $.NSEventMaskAny, $.NSDate.dateWithTimeIntervalSinceNow(0.2), $.NSDefaultRunLoopMode, true);
      if (!ev || ev.isNil()) break;
      app.sendEvent(ev);
    }
    const status = readStatus(statusPath);
    if (status === null || status === 'DONE' || status === 'FAILED') break;
    if (status === last) continue;
    last = status;
    const tab = status.indexOf('\t');
    if (tab < 0) continue;
    const pct = parseFloat(status.slice(0, tab));
    label.stringValue = status.slice(tab + 1);
    if (isNaN(pct) || pct < 0) {
      if (!bar.indeterminate) { bar.indeterminate = true; bar.startAnimation(null); }
    } else {
      if (bar.indeterminate) { bar.stopAnimation(null); bar.indeterminate = false; }
      bar.doubleValue = Math.min(100, pct);
    }
    win.displayIfNeeded;
  }
  win.orderOut(null);
}
