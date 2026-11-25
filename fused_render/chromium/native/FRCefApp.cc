#include "FRCefApp.h"

#include <map>

#include "include/base/cef_logging.h"
#include "include/cef_command_line.h"
#include "include/cef_process_message.h"
#include "include/cef_request_context.h"
#include "include/wrapper/cef_helpers.h"

namespace fused {

const char kHandlerNamesKey[] = "fused.scriptMessageHandlers";
const char kScriptMessage[] = "fused.scriptMessage";
const char kPointerLockRequest[] = "fused.pointerLock.request";
const char kPointerLockState[] = "fused.pointerLock.state";
const char kPointerLockMove[] = "fused.pointerLock.move";

namespace {

// `postMessage` of one shim: sends [name, JSON.stringify(body)] to the
// browser process from the calling frame.
class PostMessageHandler : public CefV8Handler {
 public:
  explicit PostMessageHandler(std::string name) : name_(std::move(name)) {}

  bool Execute(const CefString& function,
               CefRefPtr<CefV8Value> object,
               const CefV8ValueList& arguments,
               CefRefPtr<CefV8Value>& retval,
               CefString& exception) override {
    CefRefPtr<CefV8Context> context = CefV8Context::GetCurrentContext();
    if (!context) return false;
    CefRefPtr<CefFrame> frame = context->GetFrame();
    if (!frame) return false;
    // Serialise in JS land: JSON.stringify handles every value a page would
    // post (WebKit accepts the same set, structured-clone aside).
    CefRefPtr<CefV8Value> global = context->GetGlobal();
    CefRefPtr<CefV8Value> json = global->GetValue("JSON");
    CefString body;
    if (json && json->IsObject() && !arguments.empty()) {
      CefRefPtr<CefV8Value> stringify = json->GetValue("stringify");
      if (stringify && stringify->IsFunction()) {
        CefV8ValueList args;
        args.push_back(arguments[0]);
        CefRefPtr<CefV8Value> out = stringify->ExecuteFunction(json, args);
        if (out && out->IsString()) body = out->GetStringValue();
      }
    }
    CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create(kScriptMessage);
    CefRefPtr<CefListValue> list = msg->GetArgumentList();
    list->SetString(0, name_);
    list->SetString(1, body);
    frame->SendProcessMessage(PID_BROWSER, msg);
    retval = CefV8Value::CreateUndefined();
    return true;
  }

 private:
  std::string name_;
  IMPLEMENT_REFCOUNTING(PostMessageHandler);
};

// ---- pointer lock -----------------------------------------------------------
//
// An Alloy-style browser never gets pointer lock from CEF: the browser host
// leaves WebContentsDelegate::RequestPointerLock at Chromium's default, which
// refuses every request, and CEF has no handler to grant it. A WKWebView
// window grants it (mac_window.py), so the engine provides the API itself.
// This shim replaces `requestPointerLock` / `exitPointerLock` /
// `pointerLockElement` in every frame; the view (FRChromiumView.mm) pins and
// hides the cursor and sends the mouse's movement, which the shim dispatches
// on the locked element as `pointermove` + `mousemove` carrying
// `movementX/Y`. Those events are synthetic (`isTrusted` is false). Button
// and wheel events are real; while locked they are re-aimed at the locked
// element, as a real lock does.
//
// Evaluates to `install(send)`, which returns `deliver(kind, a, b)`:
// deliver("state", locked) and deliver("move", dx, dy).
const char kPointerLockShim[] = R"JS((function (send) {
  'use strict';
  if (typeof Element === 'undefined' || typeof Document === 'undefined') return null;
  var locked = null;    // the element holding the lock
  var pending = null;   // { el, waiters } while the view has not answered
  var exiting = false;  // the page itself asked to let go
  var byTarget = false; // ...and so may lock again without a gesture
  var x = 0, y = 0, sx = 0, sy = 0, buttons = 0;
  var RETARGET = ['mousedown', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu',
                  'pointerdown', 'pointerup', 'wheel'];

  function fire(type) { document.dispatchEvent(new Event(type, { bubbles: true })); }
  function fail(waiters, name, message) {
    fire('pointerlockerror');
    waiters.forEach(function (w) { w.reject(new DOMException(message, name)); });
  }

  // Where the cursor was when the lock was taken: what a locked event reports.
  window.addEventListener('mousemove', function (e) {
    if (!e.isTrusted || locked) return;
    x = e.clientX; y = e.clientY; sx = e.screenX; sy = e.screenY;
  }, { capture: true, passive: true });

  // The cursor is pinned wherever it was, so a real button or wheel event
  // lands on whatever sits there: hand it to the locked element instead.
  function retarget(e) {
    if (!e.isTrusted || !locked) return;
    if (typeof e.buttons === 'number') buttons = e.buttons;
    if (e.target === locked) return;
    e.stopImmediatePropagation();
    var copy = new e.constructor(e.type, e);
    var allowed = locked.dispatchEvent(copy);
    if (!allowed || e.type === 'click' || e.type === 'dblclick' || e.type === 'auxclick' ||
        e.type === 'mousedown') {
      e.preventDefault();
    }
  }
  function listen(on) {
    RETARGET.forEach(function (type) {
      if (on) window.addEventListener(type, retarget, { capture: true, passive: false });
      else window.removeEventListener(type, retarget, { capture: true });
    });
  }

  // A sandboxed frame without `allow-pointer-lock` gets no lock, as in a
  // browser: the shell's thumbnail frames (frame-focus.ts, THUMB_SEAL) count
  // on it. A cross-origin parent cannot be read, and the walk stops there.
  function sandboxed() {
    try {
      for (var w = window; w !== w.parent; w = w.parent) {
        var frame = w.frameElement;
        if (!frame) return false;
        if (frame.hasAttribute('sandbox') && !frame.sandbox.contains('allow-pointer-lock')) return true;
      }
    } catch (e) { /* cross-origin */ }
    return false;
  }

  Element.prototype.requestPointerLock = function () {
    var el = this;
    return new Promise(function (resolve, reject) {
      var waiter = { resolve: resolve, reject: reject };
      if (el.ownerDocument !== document || !el.isConnected) {
        return fail([waiter], 'WrongDocumentError', 'The element is not in this document.');
      }
      if (sandboxed()) {
        return fail([waiter], 'SecurityError', 'Blocked pointer lock on an element because the element\'s frame is sandboxed and the \'allow-pointer-lock\' permission is not set.');
      }
      if (locked) {
        if (locked !== el) { locked = el; fire('pointerlockchange'); }
        return resolve();
      }
      if (pending) { pending.el = el; pending.waiters.push(waiter); return; }
      var activation = navigator.userActivation;
      if (!byTarget && activation && !activation.isActive) {
        return fail([waiter], 'NotAllowedError', 'A user gesture is required to request Pointer Lock.');
      }
      pending = { el: el, waiters: [waiter] };
      send(true);
    });
  };
  Document.prototype.exitPointerLock = function () {
    if (this !== document || (!locked && !pending)) return;
    exiting = !!locked;
    send(false);
  };
  Object.defineProperty(Document.prototype, 'pointerLockElement', {
    configurable: true, enumerable: true,
    get: function () { return this === document ? locked : null; },
  });

  return function deliver(kind, a, b) {
    if (kind === 'state') {
      var p = pending; pending = null;
      if (a) {
        if (!p || !p.el.isConnected) {  // nobody is waiting any more: give it back
          send(false);
          if (p) fail(p.waiters, 'WrongDocumentError', 'The element left the document.');
          return;
        }
        byTarget = false; exiting = false;
        locked = p.el; buttons = 0; listen(true);
        fire('pointerlockchange');
        p.waiters.forEach(function (w) { w.resolve(); });
      } else {
        byTarget = exiting; exiting = false;
        if (p) fail(p.waiters, 'NotAllowedError', 'The pointer lock request was refused.');
        if (locked) { locked = null; listen(false); fire('pointerlockchange'); }
      }
      return;
    }
    if (!locked) return;
    if (!locked.isConnected) { send(false); return; }
    var init = { bubbles: true, cancelable: true, composed: true, view: window,
                 clientX: x, clientY: y, screenX: sx, screenY: sy,
                 movementX: a, movementY: b, button: -1, buttons: buttons };
    var pointer = { pointerId: 1, pointerType: 'mouse', isPrimary: true };
    for (var k in init) pointer[k] = init[k];
    locked.dispatchEvent(new PointerEvent('pointermove', pointer));
    init.button = 0;
    if (locked) locked.dispatchEvent(new MouseEvent('mousemove', init));
  };
}))JS";

// `send(want)` of the shim: asks the view for the lock, or lets it go.
class PointerLockHandler : public CefV8Handler {
 public:
  PointerLockHandler() = default;

  bool Execute(const CefString& function,
               CefRefPtr<CefV8Value> object,
               const CefV8ValueList& arguments,
               CefRefPtr<CefV8Value>& retval,
               CefString& exception) override {
    CefRefPtr<CefV8Context> context = CefV8Context::GetCurrentContext();
    CefRefPtr<CefFrame> frame = context ? context->GetFrame() : nullptr;
    if (!frame) return false;
    CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create(kPointerLockRequest);
    msg->GetArgumentList()->SetBool(0, !arguments.empty() && arguments[0]->GetBoolValue());
    frame->SendProcessMessage(PID_BROWSER, msg);
    retval = CefV8Value::CreateUndefined();
    return true;
  }

 private:
  IMPLEMENT_REFCOUNTING(PointerLockHandler);
};

}  // namespace

void FRCefApp::OnBeforeCommandLineProcessing(const CefString& process_type,
                                             CefRefPtr<CefCommandLine> command_line) {
  if (!process_type.empty()) return;  // browser process only
  // A desktop app, not a kiosk: no first-run / default-browser noise, and
  // media autoplay without a gesture, as the WKWebView configuration allows
  // (setMediaTypesRequiringUserActionForPlayback_(0)).
  command_line->AppendSwitchWithValue("autoplay-policy", "no-user-gesture-required");
  // Chromium's own crash handling would need a reporter; keep it quiet.
  command_line->AppendSwitch("disable-breakpad");
  // An NSWindow another window fully covers is marked occluded, and
  // Chromium then treats its page as hidden: the renderer stops producing
  // frames and the compositor drops the last one (oldest hidden widgets
  // first). The CEF child layer is empty from then on, so Mission Control
  // and the Dock's window previews showed the host view's grey background
  // for the windows covered longest. WKWebView kept its last frame; keep
  // rendering occluded windows so the engine does too.
  command_line->AppendSwitch("disable-backgrounding-occluded-windows");
  // No keychain: the cookie-encryption key is Chromium's fixed mock one,
  // not a login-keychain item. The shared item ("Chromium Safe Storage",
  // every CEF app on the Mac) threw a keychain ACL prompt on first start;
  // the name is compile-time in Chromium, so renaming is not an option.
  // The windows only ever show the local server (window_policy), whose
  // one cookie is the LAN pairing secret for remote clients; the profile
  // holds UI state, and file permissions guard it the way they guard the
  // WKWebView store today.
  command_line->AppendSwitch("use-mock-keychain");
}

void FRCefApp::OnScheduleMessagePumpWork(int64_t delay_ms) {
  if (schedule_) schedule_(delay_ms);
}

void FRCefApp::OnContextInitialized() {
  // Chromium's automatic-downloads limiter (DownloadRequestLimiter), which
  // WebKit never had. After a page's first download it asks before the next
  // one, and the answer is saved in the profile (root_cache_path persists it
  // across launches) as a content setting for the frame's origin. Before the
  // permission handler granted that prompt it denied it for any origin but
  // the local server, so every profile that ever downloaded twice from a
  // framed workbench canvas holds BLOCK for https://www.fused.io: the limiter
  // reads BLOCK and refuses the second download without asking, so granting
  // the prompt never got the chance to help. Clear those saved answers and
  // make "allow" the default, so a page downloads as often as it asks and
  // the prompt is never raised.
  CefRefPtr<CefRequestContext> context = CefRequestContext::GetGlobalContext();
  if (!context) return;
  CefString error;
  if (!context->SetPreference("profile.content_settings.exceptions.automatic_downloads",
                              nullptr, error)) {
    LOG(WARNING) << "could not clear saved automatic-downloads settings: "
                 << error.ToString();
  }
  context->SetContentSetting(CefString(), CefString(),
                             CEF_CONTENT_SETTING_TYPE_AUTOMATIC_DOWNLOADS,
                             CEF_CONTENT_SETTING_VALUE_ALLOW);
}

void FRCefApp::OnBrowserCreated(CefRefPtr<CefBrowser> browser,
                                CefRefPtr<CefDictionaryValue> extra_info) {
  std::vector<std::string> names;
  if (extra_info && extra_info->HasKey(kHandlerNamesKey)) {
    CefRefPtr<CefListValue> list = extra_info->GetList(kHandlerNamesKey);
    if (list) {
      for (size_t i = 0; i < list->GetSize(); ++i) names.push_back(list->GetString(i));
    }
  }
  handler_names_[browser->GetIdentifier()] = std::move(names);
}

void FRCefApp::OnContextCreated(CefRefPtr<CefBrowser> browser,
                                CefRefPtr<CefFrame> frame,
                                CefRefPtr<CefV8Context> context) {
  // Pointer lock, in every frame of every browser (see kPointerLockShim).
  CefRefPtr<CefV8Value> install;
  CefRefPtr<CefV8Exception> error;
  if (context->Eval(kPointerLockShim, CefString(), 0, install, error) && install &&
      install->IsFunction()) {
    CefV8ValueList args;
    args.push_back(CefV8Value::CreateFunction("send", new PointerLockHandler()));
    CefRefPtr<CefV8Value> deliver = install->ExecuteFunction(nullptr, args);
    if (deliver && deliver->IsFunction()) {
      pointer_lock_[frame->GetIdentifier().ToString()] = {context, deliver};
    }
  }

  auto it = handler_names_.find(browser->GetIdentifier());
  if (it == handler_names_.end() || it->second.empty()) return;
  // window.webkit.messageHandlers.<name>.postMessage — the shape the shell's
  // theme.ts / dock.ts already feature-detect, so the page needs no change.
  CefRefPtr<CefV8Value> global = context->GetGlobal();
  CefRefPtr<CefV8Value> webkit = CefV8Value::CreateObject(nullptr, nullptr);
  CefRefPtr<CefV8Value> handlers = CefV8Value::CreateObject(nullptr, nullptr);
  for (const std::string& name : it->second) {
    CefRefPtr<CefV8Value> handler = CefV8Value::CreateObject(nullptr, nullptr);
    CefRefPtr<CefV8Value> fn =
        CefV8Value::CreateFunction("postMessage", new PostMessageHandler(name));
    handler->SetValue("postMessage", fn, V8_PROPERTY_ATTRIBUTE_READONLY);
    handlers->SetValue(name, handler, V8_PROPERTY_ATTRIBUTE_READONLY);
  }
  webkit->SetValue("messageHandlers", handlers, V8_PROPERTY_ATTRIBUTE_READONLY);
  global->SetValue("webkit", webkit, V8_PROPERTY_ATTRIBUTE_READONLY);
}

void FRCefApp::OnContextReleased(CefRefPtr<CefBrowser> browser,
                                 CefRefPtr<CefFrame> frame,
                                 CefRefPtr<CefV8Context> context) {
  auto it = pointer_lock_.find(frame->GetIdentifier().ToString());
  // A frame's next document can get its context before the old one is
  // released: only the entry of the context going away is dropped.
  if (it == pointer_lock_.end() || !it->second.context->IsSame(context)) return;
  pointer_lock_.erase(it);
  // The document is going away: a lock it held goes with it (the view
  // ignores this from a frame that holds none).
  CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create(kPointerLockRequest);
  msg->GetArgumentList()->SetBool(0, false);
  frame->SendProcessMessage(PID_BROWSER, msg);
}

bool FRCefApp::OnProcessMessageReceived(CefRefPtr<CefBrowser> browser,
                                        CefRefPtr<CefFrame> frame,
                                        CefProcessId source_process,
                                        CefRefPtr<CefProcessMessage> message) {
  const std::string name = message->GetName().ToString();
  const bool state = name == kPointerLockState;
  if (!state && name != kPointerLockMove) return false;
  auto it = pointer_lock_.find(frame->GetIdentifier().ToString());
  if (it == pointer_lock_.end()) return true;
  CefRefPtr<CefListValue> list = message->GetArgumentList();
  CefV8ValueList args;
  // Values are created inside the frame's context.
  CefRefPtr<CefV8Context> context = it->second.context;
  if (!context->Enter()) return true;
  if (state) {
    args.push_back(CefV8Value::CreateString("state"));
    args.push_back(CefV8Value::CreateBool(list->GetBool(0)));
  } else {
    args.push_back(CefV8Value::CreateString("move"));
    args.push_back(CefV8Value::CreateDouble(list->GetDouble(0)));
    args.push_back(CefV8Value::CreateDouble(list->GetDouble(1)));
  }
  CefRefPtr<CefV8Value> deliver = it->second.deliver;
  deliver->ExecuteFunction(nullptr, args);
  context->Exit();
  return true;
}

}  // namespace fused
