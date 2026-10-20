#include "FRCefApp.h"

#include <map>

#include "include/cef_command_line.h"
#include "include/cef_process_message.h"
#include "include/wrapper/cef_helpers.h"

namespace fused {

const char kHandlerNamesKey[] = "fused.scriptMessageHandlers";
const char kScriptMessage[] = "fused.scriptMessage";

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
}

void FRCefApp::OnScheduleMessagePumpWork(int64_t delay_ms) {
  if (schedule_) schedule_(delay_ms);
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

}  // namespace fused
