// FRChromiumView: one CEF browser inside an NSView, with a CefClient that
// turns Chromium's callbacks into FRChromiumViewDelegate questions.
#import "FRChromiumView.h"
#import "FRChromiumInternal.h"

#include <map>
#include <string>
#include <vector>

#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_context_menu_handler.h"
#include "include/cef_dialog_handler.h"
#include "include/cef_display_handler.h"
#include "include/cef_download_handler.h"
#include "include/cef_jsdialog_handler.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"
#include "include/cef_parser.h"
#include "include/cef_permission_handler.h"
#include "include/cef_request_handler.h"
#include "include/wrapper/cef_helpers.h"

#include "FRCefApp.h"

// ---- helpers -----------------------------------------------------------------------

static NSString *FRString(const CefString &s) {
  std::string u = s.ToString();
  return [NSString stringWithUTF8String:u.c_str()] ?: @"";
}

static NSURL *_Nullable FRURL(const CefString &s) {
  if (s.empty()) return nil;
  return [NSURL URLWithString:FRString(s)];
}

static NSString *FRIdentifier(NSString *suffix) {
  return [@"WKMenuItemIdentifier" stringByAppendingString:suffix];
}

// Our own context-menu commands (CEF's defaults cover editing + back/forward).
enum FRMenuCommand {
  FR_MENU_OPEN_LINK = MENU_ID_USER_FIRST,
  FR_MENU_COPY_LINK,
  FR_MENU_DOWNLOAD_LINK,
  FR_MENU_OPEN_IMAGE,
  FR_MENU_COPY_IMAGE_URL,
  FR_MENU_DOWNLOAD_IMAGE,
  FR_MENU_OPEN_MEDIA,
  FR_MENU_DOWNLOAD_MEDIA,
  FR_MENU_RELOAD,
  FR_MENU_INSPECT,
};

// ---- FRNavigationAction -----------------------------------------------------------------

@interface FRNavigationAction ()
- (instancetype)initWithURL:(nullable NSURL *)url
                  mainFrame:(BOOL)mainFrame
                targetFrame:(BOOL)targetFrame
                userGesture:(BOOL)gesture
                   download:(BOOL)download
                  modifiers:(NSUInteger)modifiers
                     button:(NSInteger)button;
@end

@implementation FRNavigationAction
- (instancetype)initWithURL:(nullable NSURL *)url
                  mainFrame:(BOOL)mainFrame
                targetFrame:(BOOL)targetFrame
                userGesture:(BOOL)gesture
                   download:(BOOL)download
                  modifiers:(NSUInteger)modifiers
                     button:(NSInteger)button {
  if ((self = [super init])) {
    _URL = url;
    _isMainFrame = mainFrame;
    _hasTargetFrame = targetFrame;
    _isUserGesture = gesture;
    _isDownload = download;
    _modifierFlags = modifiers;
    _buttonNumber = button;
  }
  return self;
}
@end

// ---- the view's private surface the client calls -------------------------------------------

@interface FRChromiumView ()
@property(nonatomic, readwrite, nullable) NSURL *URL;
@property(nonatomic, readwrite, copy) NSString *title;
@property(nonatomic, readwrite) BOOL isLoading;
@property(nonatomic, readwrite) BOOL canGoBack;
@property(nonatomic, readwrite) BOOL canGoForward;
- (void)browserCreated:(CefRefPtr<CefBrowser>)browser;
- (void)browserClosed;
- (void)popupAborted;
- (void)addressChanged:(const CefString &)url;
- (CefRefPtr<CefClient>)client;
- (FRChromiumEngine *)engine;
- (cef_color_t)backgroundCefColor;
@end

// A DevTools window CEF opens for Inspect Element: its own top-level window,
// tracked by the engine so quit closes it before CefShutdown.
class FRDevToolsClient : public CefClient, public CefLifeSpanHandler {
 public:
  explicit FRDevToolsClient(FRChromiumEngine *engine) : engine_(engine) {}
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    [engine_ trackAuxiliaryBrowser:browser];
  }
  void OnBeforeClose(CefRefPtr<CefBrowser> browser) override {
    [engine_ forgetAuxiliaryBrowser:browser];
  }

 private:
  FRChromiumEngine *__weak engine_;
  IMPLEMENT_REFCOUNTING(FRDevToolsClient);
};

// ---- the CefClient ---------------------------------------------------------------------------

namespace {

class FRClient : public CefClient,
                 public CefLifeSpanHandler,
                 public CefLoadHandler,
                 public CefDisplayHandler,
                 public CefRequestHandler,
                 public CefDownloadHandler,
                 public CefJSDialogHandler,
                 public CefDialogHandler,
                 public CefPermissionHandler,
                 public CefContextMenuHandler {
 public:
  explicit FRClient(FRChromiumView *view) : view_(view) {}

  FRChromiumView *view() const { return view_; }
  id<FRChromiumViewDelegate> delegate() const { return view_ ? view_.delegate : nil; }

  // CefClient
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }
  CefRefPtr<CefDisplayHandler> GetDisplayHandler() override { return this; }
  CefRefPtr<CefRequestHandler> GetRequestHandler() override { return this; }
  CefRefPtr<CefDownloadHandler> GetDownloadHandler() override { return this; }
  CefRefPtr<CefJSDialogHandler> GetJSDialogHandler() override { return this; }
  CefRefPtr<CefDialogHandler> GetDialogHandler() override { return this; }
  CefRefPtr<CefPermissionHandler> GetPermissionHandler() override { return this; }
  CefRefPtr<CefContextMenuHandler> GetContextMenuHandler() override { return this; }

  bool OnProcessMessageReceived(CefRefPtr<CefBrowser> browser,
                                CefRefPtr<CefFrame> frame,
                                CefProcessId source_process,
                                CefRefPtr<CefProcessMessage> message) override {
    if (message->GetName() != fused::kScriptMessage) return false;
    // Only the main frame drives the window chrome (the WebKit bridge
    // checked frameInfo.isMainFrame the same way).
    if (!frame || !frame->IsMain()) return true;
    CefRefPtr<CefListValue> args = message->GetArgumentList();
    NSString *name = FRString(args->GetString(0));
    NSString *json = FRString(args->GetString(1));
    id body = nil;
    if (json.length) {
      NSData *data = [json dataUsingEncoding:NSUTF8StringEncoding];
      body = [NSJSONSerialization JSONObjectWithData:data
                                             options:NSJSONReadingFragmentsAllowed
                                               error:nil];
      if (!body) body = json;
    }
    id<FRChromiumViewDelegate> d = delegate();
    if ([d respondsToSelector:@selector(chromiumView:didReceiveScriptMessage:body:)]) {
      [d chromiumView:view_ didReceiveScriptMessage:name body:body];
    }
    return true;
  }

  // ---- CefLifeSpanHandler ----
  bool OnBeforePopup(CefRefPtr<CefBrowser> browser,
                     CefRefPtr<CefFrame> frame,
                     int popup_id,
                     const CefString &target_url,
                     const CefString &target_frame_name,
                     CefLifeSpanHandler::WindowOpenDisposition target_disposition,
                     bool user_gesture,
                     const CefPopupFeatures &popupFeatures,
                     CefWindowInfo &windowInfo,
                     CefRefPtr<CefClient> &client,
                     CefBrowserSettings &settings,
                     CefRefPtr<CefDictionaryValue> &extra_info,
                     bool *no_javascript_access) override {
    // `window.open` and target=_blank both land here. The delegate decides
    // (a page of ours gets a window; an external URL goes to the browser;
    // anything else gets nothing) and hands back a view to host the popup.
    id<FRChromiumViewDelegate> d = delegate();
    if (![d respondsToSelector:@selector(chromiumView:createViewForPopupWithURL:)]) return true;
    FRChromiumView *popup = [d chromiumView:view_ createViewForPopupWithURL:FRURL(target_url)];
    if (!popup) return true;  // cancelled
    NSRect b = popup.bounds;
    windowInfo.SetAsChild((__bridge void *)popup,
                          CefRect(0, 0, (int)NSWidth(b), (int)NSHeight(b)));
    client = [popup client];
    extra_info = ExtraInfo([popup engine]);
    settings.background_color = [popup backgroundCefColor];
    pending_popups_[popup_id] = popup;
    return false;
  }

  void OnBeforePopupAborted(CefRefPtr<CefBrowser> browser, int popup_id) override {
    // Creation failed after we handed a view out: the window the delegate
    // opened for it has nothing to show — let it close itself.
    auto it = pending_popups_.find(popup_id);
    if (it == pending_popups_.end()) return;
    FRChromiumView *popup = it->second;
    pending_popups_.erase(it);
    [popup popupAborted];
  }


  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    [view_ browserCreated:browser];
  }

  bool DoClose(CefRefPtr<CefBrowser> browser) override {
    // Two doors. `closeBrowser` (ours: the window is closing or has
    // closed): false lets CEF do its standard close — performClose: on
    // the top-level window if one is still there, a no-op otherwise, and
    // the browser is destroyed (OnBeforeClose follows). The page's own
    // `window.close()`: the delegate closes OUR window the way ⌘W does,
    // whose NSWindowWillClose notification destroys the browser — so true
    // here, or CEF would close the window a second time.
    if (closing_requested_) return false;
    id<FRChromiumViewDelegate> d = delegate();
    if ([d respondsToSelector:@selector(chromiumViewDidRequestClose:)]) {
      [d chromiumViewDidRequestClose:view_];
      return true;
    }
    return false;
  }

  void OnBeforeClose(CefRefPtr<CefBrowser> browser) override {
    [view_ browserClosed];
  }

  void set_closing_requested() { closing_requested_ = true; }

  // ---- CefLoadHandler ----
  void OnLoadingStateChange(CefRefPtr<CefBrowser> browser,
                            bool isLoading,
                            bool canGoBack,
                            bool canGoForward) override {
    view_.isLoading = isLoading;
    view_.canGoBack = canGoBack;
    view_.canGoForward = canGoForward;
  }

  void OnLoadError(CefRefPtr<CefBrowser> browser,
                   CefRefPtr<CefFrame> frame,
                   ErrorCode errorCode,
                   const CefString &errorText,
                   const CefString &failedUrl) override {
    if (errorCode == ERR_ABORTED) return;  // cancelled: a hand-off, never a failure
    if (!frame->IsMain()) return;
    id<FRChromiumViewDelegate> d = delegate();
    if ([d respondsToSelector:@selector(chromiumView:didFailNavigationWithCode:URL:description:)]) {
      [d chromiumView:view_
          didFailNavigationWithCode:(NSInteger)errorCode
                                URL:FRURL(failedUrl)
                        description:FRString(errorText)];
    }
  }

  // ---- CefDisplayHandler ----
  void OnTitleChange(CefRefPtr<CefBrowser> browser, const CefString &title) override {
    view_.title = FRString(title);
  }

  void OnAddressChange(CefRefPtr<CefBrowser> browser,
                       CefRefPtr<CefFrame> frame,
                       const CefString &url) override {
    if (frame->IsMain()) [view_ addressChanged:url];
  }

  void OnFullscreenModeChange(CefRefPtr<CefBrowser> browser, bool fullscreen) override {
    id<FRChromiumViewDelegate> d = delegate();
    if ([d respondsToSelector:@selector(chromiumView:fullscreenDidChange:)]) {
      [d chromiumView:view_ fullscreenDidChange:fullscreen];
    }
  }

  // ---- CefRequestHandler ----
  bool OnBeforeBrowse(CefRefPtr<CefBrowser> browser,
                      CefRefPtr<CefFrame> frame,
                      CefRefPtr<CefRequest> request,
                      bool user_gesture,
                      bool is_redirect) override {
    // Every navigation of every frame of this page, like WebKit's
    // decidePolicyForNavigationAction. A ⌘-click arrives through
    // OnOpenURLFromTab instead (below); here the modifiers are the last
    // mouse-down's, so a plain click on a same-page link stays "allow".
    FRNavigationAction *action = [[FRNavigationAction alloc]
        initWithURL:FRURL(request->GetURL())
          mainFrame:frame->IsMain()
        targetFrame:YES
        userGesture:user_gesture
           download:false
          modifiers:0
             button:0];
    return Decide(action) != FRNavigationPolicyAllow;  // true cancels
  }

  bool OnOpenURLFromTab(CefRefPtr<CefBrowser> browser,
                        CefRefPtr<CefFrame> frame,
                        const CefString &target_url,
                        CefRequestHandler::WindowOpenDisposition target_disposition,
                        bool user_gesture) override {
    // ⌘-click / middle-click on a link: "open in a new tab", which for a
    // window app is a new window — the policy's new_window_modifier case.
    FRNavigationAction *action = [[FRNavigationAction alloc]
        initWithURL:FRURL(target_url)
          mainFrame:YES
        targetFrame:YES
        userGesture:user_gesture
           download:false
          modifiers:NSEventModifierFlagCommand
             button:0];
    Decide(action);
    return true;  // never let Chromium open a tab it has no window for
  }

  void OnRenderProcessTerminated(CefRefPtr<CefBrowser> browser,
                                 TerminationStatus status,
                                 int error_code,
                                 const CefString &error_string) override {
    id<FRChromiumViewDelegate> d = delegate();
    if ([d respondsToSelector:@selector(chromiumViewRenderProcessDidTerminate:)]) {
      [d chromiumViewRenderProcessDidTerminate:view_];
    }
  }

  // ---- CefDownloadHandler ----
  bool CanDownload(CefRefPtr<CefBrowser> browser,
                   const CefString &url,
                   const CefString &request_method) override {
    // Chromium downloads what it cannot show and what says `attachment`;
    // the response policy gets the same say WebKit's had.
    id<FRChromiumViewDelegate> d = delegate();
    SEL sel = @selector(chromiumView:decidePolicyForResponseWithMIMEType:isMainFrame:canShow:contentDisposition:);
    if (![d respondsToSelector:sel]) return true;
    FRNavigationPolicy p = [d chromiumView:view_
          decidePolicyForResponseWithMIMEType:nil
                                  isMainFrame:YES
                                      canShow:NO
                           contentDisposition:nil];
    return p == FRNavigationPolicyDownload;
  }

  bool OnBeforeDownload(CefRefPtr<CefBrowser> browser,
                        CefRefPtr<CefDownloadItem> download_item,
                        const CefString &suggested_name,
                        CefRefPtr<CefBeforeDownloadCallback> callback) override {
    id<FRChromiumViewDelegate> d = delegate();
    SEL sel = @selector(chromiumView:destinationForDownloadWithSuggestedFilename:URL:);
    if (![d respondsToSelector:sel]) return false;
    NSString *dest = [d chromiumView:view_
        destinationForDownloadWithSuggestedFilename:FRString(suggested_name)
                                                URL:FRURL(download_item->GetURL())];
    if (!dest) return false;  // cancelled
    callback->Continue(dest.UTF8String, false);
    return true;
  }

  void OnDownloadUpdated(CefRefPtr<CefBrowser> browser,
                         CefRefPtr<CefDownloadItem> download_item,
                         CefRefPtr<CefDownloadItemCallback> callback) override {
    id<FRChromiumViewDelegate> d = delegate();
    NSString *path = FRString(download_item->GetFullPath());
    if (download_item->IsComplete()) {
      if ([d respondsToSelector:@selector(chromiumView:downloadDidFinish:)]) {
        [d chromiumView:view_ downloadDidFinish:path];
      }
    } else if (download_item->IsInterrupted()) {
      if ([d respondsToSelector:@selector(chromiumView:downloadDidFail:error:)]) {
        NSString *msg = [NSString stringWithFormat:@"interrupted (%d)",
                         (int)download_item->GetInterruptReason()];
        [d chromiumView:view_ downloadDidFail:path error:msg];
      }
    }
  }

  // ---- CefJSDialogHandler ----
  bool OnJSDialog(CefRefPtr<CefBrowser> browser,
                  const CefString &origin_url,
                  JSDialogType dialog_type,
                  const CefString &message_text,
                  const CefString &default_prompt_text,
                  CefRefPtr<CefJSDialogCallback> callback,
                  bool &suppress_message) override {
    // The delegate runs a modal NSAlert. Not from inside this callback:
    // a modal loop under CefDoMessageLoopWork would nest the pump and
    // freeze every other window. Answer on the next run-loop turn.
    id<FRChromiumViewDelegate> d = delegate();
    NSString *message = FRString(message_text);
    NSString *defaultText = FRString(default_prompt_text);
    FRChromiumView *view = view_;
    switch (dialog_type) {
      case JSDIALOGTYPE_ALERT: {
        if (![d respondsToSelector:@selector(chromiumView:runJavaScriptAlert:)]) return false;
        dispatch_async(dispatch_get_main_queue(), ^{
          [view.delegate chromiumView:view runJavaScriptAlert:message];
          callback->Continue(true, CefString());
        });
        return true;
      }
      case JSDIALOGTYPE_CONFIRM: {
        if (![d respondsToSelector:@selector(chromiumView:runJavaScriptConfirm:)]) return false;
        dispatch_async(dispatch_get_main_queue(), ^{
          BOOL ok = [view.delegate chromiumView:view runJavaScriptConfirm:message];
          callback->Continue(ok, CefString());
        });
        return true;
      }
      case JSDIALOGTYPE_PROMPT: {
        if (![d respondsToSelector:@selector(chromiumView:runJavaScriptPrompt:defaultText:)]) {
          return false;
        }
        dispatch_async(dispatch_get_main_queue(), ^{
          NSString *answer = [view.delegate chromiumView:view
                                     runJavaScriptPrompt:message
                                             defaultText:defaultText];
          callback->Continue(answer != nil, answer ? CefString(answer.UTF8String) : CefString());
        });
        return true;
      }
      default:
        break;
    }
    return false;  // Chromium's own dialog
  }

  bool OnBeforeUnloadDialog(CefRefPtr<CefBrowser> browser,
                            const CefString &message_text,
                            bool is_reload,
                            CefRefPtr<CefJSDialogCallback> callback) override {
    // A browser tab asks "Leave page?"; our windows never did (WebKit
    // offers no such hook). Leave without asking, as before.
    callback->Continue(true, CefString());
    return true;
  }

  // ---- CefDialogHandler ----
  bool OnFileDialog(CefRefPtr<CefBrowser> browser,
                    FileDialogMode mode,
                    const CefString &title,
                    const CefString &default_file_path,
                    const std::vector<CefString> &accept_filters,
                    const std::vector<CefString> &accept_extensions,
                    const std::vector<CefString> &accept_descriptions,
                    CefRefPtr<CefFileDialogCallback> callback) override {
    if (mode == FILE_DIALOG_SAVE) return false;  // Chromium's save panel
    id<FRChromiumViewDelegate> d = delegate();
    if (![d respondsToSelector:@selector(chromiumView:runOpenPanelAllowingMultiple:directories:)]) {
      return false;
    }
    FRChromiumView *view = view_;
    BOOL multiple = (mode == FILE_DIALOG_OPEN_MULTIPLE);
    BOOL folders = (mode == FILE_DIALOG_OPEN_FOLDER);
    // Same as the JS dialogs: the modal panel runs on the next turn.
    dispatch_async(dispatch_get_main_queue(), ^{
      NSArray<NSString *> *paths = [view.delegate chromiumView:view
                                  runOpenPanelAllowingMultiple:multiple
                                                   directories:folders];
      if (!paths) {
        callback->Cancel();
      } else {
        std::vector<CefString> out;
        for (NSString *p in paths) out.push_back(p.UTF8String);
        callback->Continue(out);
      }
    });
    return true;
  }

  // ---- CefPermissionHandler ----
  bool OnRequestMediaAccessPermission(CefRefPtr<CefBrowser> browser,
                                      CefRefPtr<CefFrame> frame,
                                      const CefString &requesting_origin,
                                      uint32_t requested_permissions,
                                      CefRefPtr<CefMediaAccessCallback> callback) override {
    // getUserMedia is all or nothing (a partial grant is rejected by CEF):
    // every requested kind must pass the delegate.
    bool ok = true;
    if (requested_permissions & (CEF_MEDIA_PERMISSION_DEVICE_AUDIO_CAPTURE |
                                 CEF_MEDIA_PERMISSION_DESKTOP_AUDIO_CAPTURE)) {
      ok = ok && Allow(@"microphone", requesting_origin);
    }
    if (requested_permissions & (CEF_MEDIA_PERMISSION_DEVICE_VIDEO_CAPTURE |
                                 CEF_MEDIA_PERMISSION_DESKTOP_VIDEO_CAPTURE)) {
      ok = ok && Allow(@"camera", requesting_origin);
    }
    callback->Continue(ok ? requested_permissions : CEF_MEDIA_PERMISSION_NONE);
    return true;
  }

  bool OnShowPermissionPrompt(CefRefPtr<CefBrowser> browser,
                              uint64_t prompt_id,
                              const CefString &requesting_origin,
                              uint32_t requested_permissions,
                              CefRefPtr<CefPermissionPromptCallback> callback) override {
    NSString *kind = @"other";
    if (requested_permissions & CEF_PERMISSION_TYPE_GEOLOCATION) kind = @"geolocation";
    else if (requested_permissions & CEF_PERMISSION_TYPE_NOTIFICATIONS) kind = @"notifications";
    else if (requested_permissions & CEF_PERMISSION_TYPE_CLIPBOARD) kind = @"clipboard";
    else if (requested_permissions & CEF_PERMISSION_TYPE_MIDI_SYSEX) kind = @"midi";
    else if (requested_permissions & CEF_PERMISSION_TYPE_CAMERA_STREAM) kind = @"camera";
    else if (requested_permissions & CEF_PERMISSION_TYPE_MIC_STREAM) kind = @"microphone";
    bool ok = Allow(kind, requesting_origin);
    callback->Continue(ok ? CEF_PERMISSION_RESULT_ACCEPT : CEF_PERMISSION_RESULT_DENY);
    return true;
  }

  // ---- CefContextMenuHandler ----
  void OnBeforeContextMenu(CefRefPtr<CefBrowser> browser,
                           CefRefPtr<CefFrame> frame,
                           CefRefPtr<CefContextMenuParams> params,
                           CefRefPtr<CefMenuModel> model) override {
    // CEF's default menu is editing + back/forward/print/view-source. The
    // link, image and media items a browser shows are added here, then the
    // delegate gets every item under WebKit's identifier to keep, retitle
    // or drop — the same curation WebKit's menu gets.
    int type = params->GetTypeFlags();
    int media = params->GetMediaType();
    CefString link = params->GetLinkUrl();
    CefString source = params->GetSourceUrl();
    bool is_link = (type & CM_TYPEFLAG_LINK) && !link.empty();
    bool is_image = media == CM_MEDIATYPE_IMAGE && !source.empty();
    bool is_media = (media == CM_MEDIATYPE_VIDEO || media == CM_MEDIATYPE_AUDIO) && !source.empty();
    bool editable = (type & CM_TYPEFLAG_EDITABLE) != 0;

    // Drop View Source and Print from the page menu (⌘P prints; the shell
    // is not somebody's page to view source of), keep Back/Forward.
    model->Remove(MENU_ID_VIEW_SOURCE);
    model->Remove(MENU_ID_PRINT);

    if (is_link) {
      if (model->GetCount() > 0) model->AddSeparator();
      model->AddItem(FR_MENU_OPEN_LINK, "Open Link in New Window");
      model->AddItem(FR_MENU_DOWNLOAD_LINK, "Download Linked File");
      model->AddItem(FR_MENU_COPY_LINK, "Copy Link");
    }
    if (is_image) {
      if (model->GetCount() > 0) model->AddSeparator();
      model->AddItem(FR_MENU_OPEN_IMAGE, "Open Image in New Window");
      model->AddItem(FR_MENU_DOWNLOAD_IMAGE, "Download Image");
      model->AddItem(FR_MENU_COPY_IMAGE_URL, "Copy Image Address");
    }
    if (is_media) {
      if (model->GetCount() > 0) model->AddSeparator();
      model->AddItem(FR_MENU_OPEN_MEDIA, "Open Media in New Window");
      model->AddItem(FR_MENU_DOWNLOAD_MEDIA, "Download Media");
    }
    if (!editable && !is_link && !is_image && !is_media && model->GetCount() == 0) {
      model->AddItem(FR_MENU_RELOAD, "Reload");
    }
    if (model->GetCount() > 0) model->AddSeparator();
    model->AddItem(FR_MENU_INSPECT, "Inspect Element");

    id<FRChromiumViewDelegate> d = delegate();
    SEL sel = @selector(chromiumView:titleForContextMenuItem:linkURL:imageURL:mediaURL:proposedTitle:);
    if (![d respondsToSelector:sel]) return;
    NSURL *linkURL = is_link ? FRURL(link) : nil;
    NSURL *imageURL = is_image ? FRURL(source) : nil;
    NSURL *mediaURL = is_media ? FRURL(source) : nil;
    for (size_t i = model->GetCount(); i-- > 0;) {
      if (model->GetTypeAt(i) != MENUITEMTYPE_COMMAND) continue;
      int cmd = model->GetCommandIdAt(i);
      NSString *ident = IdentifierFor(cmd);
      if (!ident) continue;
      NSString *title = FRString(model->GetLabelAt(i));
      NSString *kept = [d chromiumView:view_
               titleForContextMenuItem:ident
                               linkURL:linkURL
                              imageURL:imageURL
                              mediaURL:mediaURL
                         proposedTitle:title];
      if (!kept) {
        model->RemoveAt(i);
      } else if (![kept isEqualToString:title]) {
        model->SetLabelAt(i, kept.UTF8String);
      }
    }
    // No two separators in a row, none at the edges.
    for (size_t i = model->GetCount(); i-- > 0;) {
      if (model->GetTypeAt(i) != MENUITEMTYPE_SEPARATOR) continue;
      if (i == 0 || i == model->GetCount() - 1 ||
          model->GetTypeAt(i - 1) == MENUITEMTYPE_SEPARATOR) {
        model->RemoveAt(i);
      }
    }
  }

  bool OnContextMenuCommand(CefRefPtr<CefBrowser> browser,
                            CefRefPtr<CefFrame> frame,
                            CefRefPtr<CefContextMenuParams> params,
                            int command_id,
                            EventFlags event_flags) override {
    CefString link = params->GetLinkUrl();
    CefString source = params->GetSourceUrl();
    switch (command_id) {
      case FR_MENU_OPEN_LINK:
      case FR_MENU_OPEN_IMAGE:
      case FR_MENU_OPEN_MEDIA: {
        // Same door as window.open: the delegate decides window / browser.
        const CefString &url = command_id == FR_MENU_OPEN_LINK ? link : source;
        id<FRChromiumViewDelegate> d = delegate();
        if ([d respondsToSelector:@selector(chromiumView:createViewForPopupWithURL:)]) {
          FRChromiumView *popup = [d chromiumView:view_ createViewForPopupWithURL:FRURL(url)];
          if (popup) [popup loadRequest:[NSURLRequest requestWithURL:FRURL(url)]];
        }
        return true;
      }
      case FR_MENU_COPY_LINK:
        CopyString(FRString(link));
        return true;
      case FR_MENU_COPY_IMAGE_URL:
        CopyString(FRString(source));
        return true;
      case FR_MENU_DOWNLOAD_LINK:
        browser->GetHost()->StartDownload(link);
        return true;
      case FR_MENU_DOWNLOAD_IMAGE:
      case FR_MENU_DOWNLOAD_MEDIA:
        browser->GetHost()->StartDownload(source);
        return true;
      case FR_MENU_RELOAD:
        browser->Reload();
        return true;
      case FR_MENU_INSPECT: {
        CefWindowInfo info;
        CefBrowserSettings settings;
        CefPoint at(params->GetXCoord(), params->GetYCoord());
        browser->GetHost()->ShowDevTools(info, new FRDevToolsClient([view_ engine]), settings, at);
        return true;
      }
      default:
        return false;
    }
  }

  static CefRefPtr<CefDictionaryValue> ExtraInfo(FRChromiumEngine *engine) {
    CefRefPtr<CefDictionaryValue> info = CefDictionaryValue::Create();
    CefRefPtr<CefListValue> names = CefListValue::Create();
    size_t i = 0;
    for (NSString *n in [engine scriptMessageHandlerNames]) names->SetString(i++, n.UTF8String);
    info->SetList(fused::kHandlerNamesKey, names);
    return info;
  }

 private:
  FRNavigationPolicy Decide(FRNavigationAction *action) {
    id<FRChromiumViewDelegate> d = delegate();
    if (![d respondsToSelector:@selector(chromiumView:decidePolicyForNavigationAction:)]) {
      return FRNavigationPolicyAllow;
    }
    return [d chromiumView:view_ decidePolicyForNavigationAction:action];
  }

  bool Allow(NSString *kind, const CefString &origin) {
    id<FRChromiumViewDelegate> d = delegate();
    if (![d respondsToSelector:@selector(chromiumView:allowPermission:forOrigin:)]) return false;
    return [d chromiumView:view_ allowPermission:kind forOrigin:FRURL(origin)];
  }

  static NSString *_Nullable IdentifierFor(int cmd) {
    switch (cmd) {
      case FR_MENU_OPEN_LINK: return FRIdentifier(@"OpenLinkInNewWindow");
      case FR_MENU_COPY_LINK: return FRIdentifier(@"CopyLink");
      case FR_MENU_DOWNLOAD_LINK: return FRIdentifier(@"DownloadLinkedFile");
      case FR_MENU_OPEN_IMAGE: return FRIdentifier(@"OpenImageInNewWindow");
      case FR_MENU_COPY_IMAGE_URL: return FRIdentifier(@"CopyImageURL");
      case FR_MENU_DOWNLOAD_IMAGE: return FRIdentifier(@"DownloadImage");
      case FR_MENU_OPEN_MEDIA: return FRIdentifier(@"OpenMediaInNewWindow");
      case FR_MENU_DOWNLOAD_MEDIA: return FRIdentifier(@"DownloadMedia");
      case FR_MENU_RELOAD: return FRIdentifier(@"Reload");
      case FR_MENU_INSPECT: return FRIdentifier(@"InspectElement");
      case MENU_ID_BACK: return FRIdentifier(@"GoBack");
      case MENU_ID_FORWARD: return FRIdentifier(@"GoForward");
      case MENU_ID_UNDO: return FRIdentifier(@"Undo");
      case MENU_ID_REDO: return FRIdentifier(@"Redo");
      case MENU_ID_CUT: return FRIdentifier(@"Cut");
      case MENU_ID_COPY: return FRIdentifier(@"Copy");
      case MENU_ID_PASTE: return FRIdentifier(@"Paste");
      case MENU_ID_DELETE: return FRIdentifier(@"Delete");
      case MENU_ID_SELECT_ALL: return FRIdentifier(@"SelectAll");
      default: return nil;
    }
  }

  static void CopyString(NSString *s) {
    NSPasteboard *pb = [NSPasteboard generalPasteboard];
    [pb clearContents];
    [pb setString:s forType:NSPasteboardTypeString];
  }

  FRChromiumView *__weak view_;
  bool closing_requested_ = false;
  // Weak: a popup that was created and closed normally leaves a dead entry
  // behind, read only on abort (ids are unique per opener).
  std::map<int, __weak FRChromiumView *> pending_popups_;

  IMPLEMENT_REFCOUNTING(FRClient);
  DISALLOW_COPY_AND_ASSIGN(FRClient);
};

}  // namespace

// ---- FRChromiumView ------------------------------------------------------------------

@implementation FRChromiumView {
  FRChromiumEngine *_engine;
  CefRefPtr<FRClient> _client;
  CefRefPtr<CefBrowser> _browser;
  NSURL *_placeholderBase;   // reported as URL while a loadHTMLString page shows
  NSString *_placeholderURL; // the data: URL carrying it
  NSURLRequest *_pendingRequest;  // a load asked for before the browser exists
  BOOL _creating;
  BOOL _closing;
  BOOL _closeWhenCreated;  // closeBrowser before OnAfterCreated
  id _fullscreenObserver;
}

@synthesize URL = _URL;
@synthesize title = _title;

- (instancetype)initWithFrame:(NSRect)frame engine:(FRChromiumEngine *)engine {
  if ((self = [super initWithFrame:frame])) {
    _engine = engine;
    _title = @"";
    _drawsBackground = YES;
    _client = new FRClient(self);
    self.autoresizesSubviews = YES;
    self.wantsLayer = YES;
    [engine registerView:self];
    // The user leaving macOS full screen with the window button: the page
    // is told to leave its own (Esc would have done both).
    __weak FRChromiumView *weakSelf = self;
    _fullscreenObserver = [[NSNotificationCenter defaultCenter]
        addObserverForName:NSWindowDidExitFullScreenNotification
                    object:nil
                     queue:nil
                usingBlock:^(NSNotification *note) {
      FRChromiumView *me = weakSelf;
      if (me && me->_browser && note.object == me.window) {
        me->_browser->GetHost()->ExitFullscreen(true);
      }
    }];
  }
  return self;
}

- (void)dealloc {
  if (_fullscreenObserver) [[NSNotificationCenter defaultCenter] removeObserver:_fullscreenObserver];
  [_engine unregisterView:self];
}

- (BOOL)isFlipped {
  return YES;  // CEF's browser view expects a top-left origin parent
}

- (CefRefPtr<CefClient>)client {
  return _client;
}

- (FRChromiumEngine *)engine {
  return _engine;
}

- (BOOL)hasBrowser {
  return _browser != nullptr;
}

+ (BOOL)automaticallyNotifiesObserversOfURL {
  return NO;
}

- (void)setURL:(nullable NSURL *)url {
  [self willChangeValueForKey:@"URL"];
  _URL = url;
  [self didChangeValueForKey:@"URL"];
}

- (void)setDrawsBackground:(BOOL)draws {
  // Accepted for WKWebView parity. A windowed Chromium browser is always
  // opaque, so "no background" means "the window's own colour" here: the
  // browser's first frame then matches the theme instead of flashing white.
  _drawsBackground = draws;
  self.layer.backgroundColor = draws ? NSColor.windowBackgroundColor.CGColor : NSColor.clearColor.CGColor;
}

- (cef_color_t)backgroundCefColor {
  NSColor *c = self.window.backgroundColor ?: NSColor.windowBackgroundColor;
  c = [c colorUsingColorSpace:NSColorSpace.sRGBColorSpace] ?: c;
  CGFloat r = 0, g = 0, b = 0, a = 1;
  @try {
    [c getRed:&r green:&g blue:&b alpha:&a];
  } @catch (NSException *e) {
    r = g = b = 0.07;  // the dark `--bg`; a mismatch costs one frame's tint
  }
  return CefColorSetARGB(255, (int)(r * 255), (int)(g * 255), (int)(b * 255));
}

// ---- browser lifecycle ----

- (void)createBrowserWithURL:(NSString *)url {
  CefWindowInfo info;
  NSRect b = self.bounds;
  info.SetAsChild((__bridge void *)self, CefRect(0, 0, (int)NSWidth(b), (int)NSHeight(b)));
  CefBrowserSettings settings;
  settings.background_color = [self backgroundCefColor];
  // Asynchronous: OnAfterCreated hands the browser back (browserCreated:).
  CefBrowserHost::CreateBrowser(info, _client, url.UTF8String, settings,
                                FRClient::ExtraInfo(_engine), nullptr);
}

- (void)browserCreated:(CefRefPtr<CefBrowser>)browser {
  _browser = browser;
  _creating = NO;
  [self layoutBrowserView];
  if (_closeWhenCreated) {
    // closeBrowser ran before the browser existed (a window closed during
    // the ~100 ms of creation): close it now, or its renderer would live on
    // in a detached view.
    _closeWhenCreated = NO;
    [self closeBrowser];
    return;
  }
  [self focusBrowserView];
  if (_pendingRequest) {
    NSURLRequest *req = _pendingRequest;
    _pendingRequest = nil;
    [self loadRequest:req];
  }
}

- (void)browserClosed {
  _browser = nullptr;
  _closing = YES;
  [_engine unregisterView:self];
}

- (void)popupAborted {
  _creating = NO;
  _closing = YES;
  id<FRChromiumViewDelegate> d = self.delegate;
  if ([d respondsToSelector:@selector(chromiumViewDidRequestClose:)]) {
    [d chromiumViewDidRequestClose:self];
  }
}

- (void)focusBrowserView {
  // Keyboard input and the Edit menu's copy:/paste: go to Chromium's own
  // view; this NSView is only its host.
  if (!_browser || !self.window) return;
  NSView *bv = (__bridge NSView *)_browser->GetHost()->GetWindowHandle();
  if (bv) [self.window makeFirstResponder:bv];
}

- (void)addressChanged:(const CefString &)url {
  NSString *s = FRString(url);
  if (_placeholderBase && _placeholderURL && [s isEqualToString:_placeholderURL]) {
    self.URL = _placeholderBase;
    return;
  }
  _placeholderBase = nil;
  _placeholderURL = nil;
  self.URL = FRURL(url);
}

- (void)layoutBrowserView {
  if (!_browser) return;
  NSView *bv = (__bridge NSView *)_browser->GetHost()->GetWindowHandle();
  if (bv) {
    bv.frame = self.bounds;
    bv.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  }
}

- (void)setFrameSize:(NSSize)newSize {
  [super setFrameSize:newSize];
  [self layoutBrowserView];
  if (_browser) _browser->GetHost()->NotifyMoveOrResizeStarted();
}

- (void)viewDidMoveToWindow {
  [super viewDidMoveToWindow];
  [self layoutBrowserView];
  [self focusBrowserView];
}

// ---- WKWebView-shaped API ----

- (void)loadRequest:(NSURLRequest *)request {
  NSString *url = request.URL.absoluteString ?: @"about:blank";
  _placeholderBase = nil;
  _placeholderURL = nil;
  if (_browser) {
    _browser->GetMainFrame()->LoadURL(url.UTF8String);
    return;
  }
  if (_closing) return;
  // The browser is born on its first URL (CreateBrowser is asynchronous);
  // a second request before OnAfterCreated waits for it.
  if (_creating) {
    _pendingRequest = request;
    return;
  }
  _creating = YES;
  [self createBrowserWithURL:url];
}

- (void)loadHTMLString:(NSString *)html baseURL:(nullable NSURL *)baseURL {
  // CEF has no LoadString since 97: a data: URL carries the page, and the
  // view reports `baseURL` as its URL while it shows (addressChanged:).
  NSData *data = [html dataUsingEncoding:NSUTF8StringEncoding];
  NSString *dataURL = [NSString stringWithFormat:@"data:text/html;base64,%@",
                       [data base64EncodedStringWithOptions:0]];
  [self loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:dataURL]]];
  _placeholderBase = baseURL;
  _placeholderURL = dataURL;
  if (baseURL) self.URL = baseURL;
}

- (void)reload {
  if (_browser) _browser->Reload();
}

- (void)goBack {
  if (_browser) _browser->GoBack();
}

- (void)goForward {
  if (_browser) _browser->GoForward();
}

- (void)stopLoading {
  if (_browser) _browser->StopLoad();
}

- (void)evaluateJavaScript:(NSString *)script
         completionHandler:(nullable void (^)(id _Nullable, NSError *_Nullable))handler {
  if (_browser) {
    _browser->GetMainFrame()->ExecuteJavaScript(script.UTF8String,
                                                _browser->GetMainFrame()->GetURL(), 0);
  }
  if (handler) handler(nil, nil);
}

- (void)printPage {
  if (_browser) _browser->GetHost()->Print();
}

- (void)showDevTools {
  if (!_browser) return;
  CefWindowInfo info;
  CefBrowserSettings settings;
  _browser->GetHost()->ShowDevTools(info, new FRDevToolsClient(_engine), settings, CefPoint());
}

- (void)closeBrowser {
  if (_closing) return;
  if (!_browser) {
    if (_creating) _closeWhenCreated = YES;  // honoured in browserCreated:
    return;
  }
  _closing = YES;
  _client->set_closing_requested();
  _browser->GetHost()->CloseBrowser(true);
}

- (BOOL)acceptsFirstResponder {
  return NO;  // Chromium's view is the responder (focusBrowserView)
}

@end
