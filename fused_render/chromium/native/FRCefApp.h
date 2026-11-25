// The CefApp shared by the browser process (inside FusedChromium.framework)
// and the helper processes (FRHelperMain.mm). Browser side: the external
// message pump. Render side: the `window.webkit.messageHandlers.<name>`
// shim that turns a page's postMessage into a process message the view's
// client forwards to its delegate, and the pointer-lock shim (FRCefApp.cc).
#pragma once

#include <functional>
#include <map>
#include <string>
#include <vector>

#include "include/cef_app.h"
#include "include/cef_browser_process_handler.h"
#include "include/cef_render_process_handler.h"
#include "include/cef_v8.h"

namespace fused {

// extra_info key carrying the handler names to the renderer, and the process
// message a shim sends back: [name, json-body].
extern const char kHandlerNamesKey[];
extern const char kScriptMessage[];
// Pointer lock (the shim in FRCefApp.cc, the cursor capture in
// FRChromiumView.mm). A frame asks or lets go: [bool want]. The view answers,
// and reports every later loss: [bool locked]. While locked it sends the
// mouse's movement: [dx, dy] in points.
extern const char kPointerLockRequest[];
extern const char kPointerLockState[];
extern const char kPointerLockMove[];

class FRCefApp : public CefApp,
                 public CefBrowserProcessHandler,
                 public CefRenderProcessHandler {
 public:
  // Browser process: called on the UI thread when CEF wants
  // CefDoMessageLoopWork() run after `delay_ms` (<=0: now).
  using ScheduleFn = std::function<void(int64_t delay_ms)>;
  explicit FRCefApp(ScheduleFn schedule = nullptr) : schedule_(std::move(schedule)) {}

  // CefApp
  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override { return this; }
  CefRefPtr<CefRenderProcessHandler> GetRenderProcessHandler() override { return this; }
  void OnBeforeCommandLineProcessing(const CefString& process_type,
                                     CefRefPtr<CefCommandLine> command_line) override;

  // CefBrowserProcessHandler
  void OnScheduleMessagePumpWork(int64_t delay_ms) override;
  // Profile settings the windows need before the first page loads (the
  // automatic-downloads limiter; see the definition).
  void OnContextInitialized() override;
  // A second process started on the same profile (a relaunch successor
  // racing the predecessor): the default would open a Chrome-style window
  // in THIS process; true says "handled" (the successor falls back to
  // WebKit until this one is gone).
  bool OnAlreadyRunningAppRelaunch(CefRefPtr<CefCommandLine> command_line,
                                   const CefString& current_directory) override {
    return true;
  }

  // CefRenderProcessHandler
  void OnBrowserCreated(CefRefPtr<CefBrowser> browser,
                        CefRefPtr<CefDictionaryValue> extra_info) override;
  void OnBrowserDestroyed(CefRefPtr<CefBrowser> browser) override {
    handler_names_.erase(browser->GetIdentifier());
  }
  void OnContextCreated(CefRefPtr<CefBrowser> browser,
                        CefRefPtr<CefFrame> frame,
                        CefRefPtr<CefV8Context> context) override;
  void OnContextReleased(CefRefPtr<CefBrowser> browser,
                         CefRefPtr<CefFrame> frame,
                         CefRefPtr<CefV8Context> context) override;
  bool OnProcessMessageReceived(CefRefPtr<CefBrowser> browser,
                                CefRefPtr<CefFrame> frame,
                                CefProcessId source_process,
                                CefRefPtr<CefProcessMessage> message) override;

 private:
  ScheduleFn schedule_;
  // Render process: handler names per browser id (OnBrowserCreated).
  std::map<int, std::vector<std::string>> handler_names_;
  // Render process: the pointer-lock shim's `deliver` function per frame id,
  // with the context it belongs to (dropped in OnContextReleased).
  struct PointerLockShim {
    CefRefPtr<CefV8Context> context;
    CefRefPtr<CefV8Value> deliver;
  };
  std::map<std::string, PointerLockShim> pointer_lock_;

  IMPLEMENT_REFCOUNTING(FRCefApp);
  DISALLOW_COPY_AND_ASSIGN(FRCefApp);
};

}  // namespace fused
