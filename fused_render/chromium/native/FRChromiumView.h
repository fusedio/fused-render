// FusedChromium.framework — the Chromium Embedded Framework wrapped as one
// NSView whose surface mirrors the WKWebView selectors fused-render's window
// layer (mac_window.py) uses, plus one delegate protocol asking the same
// questions WebKit's navigation / UI / download delegates ask.
//
// Loaded from Python (PyObjC, chromium/mac.py) out of the on-demand engine
// directory; the Python side answers the delegate. Nothing here knows about
// fused-render's URLs or policy — that stays in window_policy.py.
//
// Threading: everything is main-thread. CEF runs with an external message
// pump that FRChromiumEngine drives from the main run loop.

#import <Cocoa/Cocoa.h>

NS_ASSUME_NONNULL_BEGIN

@class FRChromiumView;

/// What to do with a navigation (values match chromium/mac.py).
typedef NS_ENUM(NSInteger, FRNavigationPolicy) {
  FRNavigationPolicyAllow = 0,
  FRNavigationPolicyCancel = 1,
  FRNavigationPolicyDownload = 2,
};

/// One navigation Chromium is about to perform — the WKNavigationAction fields
/// window_policy.navigation_action() reads.
@interface FRNavigationAction : NSObject
@property(nonatomic, readonly, nullable) NSURL *URL;
@property(nonatomic, readonly) BOOL isMainFrame;
/// NO for a navigation aimed at no frame of this page (target=_blank style).
@property(nonatomic, readonly) BOOL hasTargetFrame;
@property(nonatomic, readonly) BOOL isUserGesture;
/// YES when the navigation carries a download disposition.
@property(nonatomic, readonly) BOOL isDownload;
/// NSEvent modifier flags of the click that started it (0 when unknown).
@property(nonatomic, readonly) NSUInteger modifierFlags;
/// 0 left, 1 right, 2 middle (as WKNavigationAction.buttonNumber).
@property(nonatomic, readonly) NSInteger buttonNumber;
@end

@protocol FRChromiumViewDelegate <NSObject>
@optional
- (FRNavigationPolicy)chromiumView:(FRChromiumView *)view
    decidePolicyForNavigationAction:(FRNavigationAction *)action;
- (FRNavigationPolicy)chromiumView:(FRChromiumView *)view
    decidePolicyForResponseWithMIMEType:(nullable NSString *)mimeType
                            isMainFrame:(BOOL)isMainFrame
                                canShow:(BOOL)canShow
                     contentDisposition:(nullable NSString *)disposition;
/// `window.open` / target=_blank: return a fresh FRChromiumView (not yet
/// attached to a browser) to host the popup, or nil for no popup.
- (nullable FRChromiumView *)chromiumView:(FRChromiumView *)view
               createViewForPopupWithURL:(nullable NSURL *)url;
/// The absolute path to save a download at, or nil to cancel it.
- (nullable NSString *)chromiumView:(FRChromiumView *)view
    destinationForDownloadWithSuggestedFilename:(NSString *)filename
                                            URL:(nullable NSURL *)url;
- (void)chromiumView:(FRChromiumView *)view downloadDidFinish:(NSString *)path;
- (void)chromiumView:(FRChromiumView *)view
     downloadDidFail:(NSString *)path
               error:(NSString *)message;
- (void)chromiumView:(FRChromiumView *)view runJavaScriptAlert:(NSString *)message;
- (BOOL)chromiumView:(FRChromiumView *)view runJavaScriptConfirm:(NSString *)message;
- (nullable NSString *)chromiumView:(FRChromiumView *)view
                runJavaScriptPrompt:(NSString *)message
                        defaultText:(nullable NSString *)defaultText;
/// `<input type=file>`: absolute paths, or nil for cancel.
- (nullable NSArray<NSString *> *)chromiumView:(FRChromiumView *)view
                 runOpenPanelAllowingMultiple:(BOOL)multiple
                                  directories:(BOOL)directories;
/// kind: "camera", "microphone", "geolocation", "notifications", "clipboard",
/// "midi", "other". YES grants.
- (BOOL)chromiumView:(FRChromiumView *)view
     allowPermission:(NSString *)kind
           forOrigin:(nullable NSURL *)origin;
/// The right-click menu, one item at a time, under WebKit's identifiers
/// (WKMenuItemIdentifierOpenLinkInNewWindow, …Copy, …Reload, …). Return
/// the title to show, or nil to drop the item.
- (nullable NSString *)chromiumView:(FRChromiumView *)view
            titleForContextMenuItem:(NSString *)identifier
                            linkURL:(nullable NSURL *)linkURL
                           imageURL:(nullable NSURL *)imageURL
                           mediaURL:(nullable NSURL *)mediaURL
                      proposedTitle:(NSString *)title;
/// `window.webkit.messageHandlers.<name>.postMessage(body)` from the page's
/// MAIN frame. `body` is the JSON-decoded value (NSString/NSNumber/…).
- (void)chromiumView:(FRChromiumView *)view
    didReceiveScriptMessage:(NSString *)name
                       body:(nullable id)body;
/// The page called `window.close()`.
- (void)chromiumViewDidRequestClose:(FRChromiumView *)view;
/// The renderer process died; the view shows nothing until reloaded.
- (void)chromiumViewRenderProcessDidTerminate:(FRChromiumView *)view;
- (void)chromiumView:(FRChromiumView *)view
    didFailNavigationWithCode:(NSInteger)code
                          URL:(nullable NSURL *)url
                  description:(NSString *)description;
/// `requestFullscreen()` entered (YES) or left (NO).
- (void)chromiumView:(FRChromiumView *)view fullscreenDidChange:(BOOL)fullscreen;
@end

/// The one engine per process. CEF initialises once and cannot restart.
@interface FRChromiumEngine : NSObject
/// Start CEF from the unpacked engine directory (holding `Chromium Embedded
/// Framework.framework` and the `FusedRender Helper*.app` bundles). Returns
/// nil on failure; `+lastError` says why. `cachePath` is the profile dir
/// (one for every view). `product` is the app's user-agent marker
/// ("FusedRender/1.2.3"); the engine keeps a `Chrome/<major>` token in
/// front of it, since CEF's product setting replaces Chromium's own.
/// `handlerNames` are the
/// `window.webkit.messageHandlers.<name>` shims every page gets.
+ (nullable instancetype)startWithEngineDirectory:(NSString *)engineDir
                                        cachePath:(NSString *)cachePath
                                 userAgentProduct:(NSString *)product
                        scriptMessageHandlerNames:(NSArray<NSString *> *)handlerNames;
+ (nullable instancetype)shared;
+ (nullable NSString *)lastError;
@property(nonatomic, readonly) NSString *cefVersion;
@property(nonatomic, readonly) NSUInteger browserCount;
/// Close every browser and shut CEF down. After this nothing works; call
/// at quit only.
- (void)shutdown;
@end

/// The view. Create it, put it in a window, then `loadRequest:` (that
/// creates the browser). KVO-compliant `URL` and `title`.
@interface FRChromiumView : NSView
- (instancetype)initWithFrame:(NSRect)frame engine:(FRChromiumEngine *)engine;
@property(nonatomic, weak, nullable) id<FRChromiumViewDelegate> delegate;
@property(nonatomic, readonly, nullable) NSURL *URL;
@property(nonatomic, readonly, copy) NSString *title;
@property(nonatomic, readonly) BOOL isLoading;
@property(nonatomic, readonly) BOOL canGoBack;
@property(nonatomic, readonly) BOOL canGoForward;
/// NO = transparent until the page paints (the window background shows).
@property(nonatomic) BOOL drawsBackground;
/// Accepted for WKWebView parity; Chromium handles swipe navigation itself.
@property(nonatomic) BOOL allowsBackForwardNavigationGestures;
- (void)loadRequest:(NSURLRequest *)request;
/// Show `html` while reporting `baseURL` as the view's URL (a placeholder
/// page until the real one is loaded).
- (void)loadHTMLString:(NSString *)html baseURL:(nullable NSURL *)baseURL;
- (void)reload;
- (void)goBack;
- (void)goForward;
- (void)stopLoading;
/// Runs `script` in the main frame. CEF cannot return a value: the handler,
/// if any, is called with (nil, nil) once the script was dispatched.
- (void)evaluateJavaScript:(NSString *)script
         completionHandler:(nullable void (^)(id _Nullable, NSError *_Nullable))handler;
/// Chromium's print flow (system panel included) for the main frame.
- (void)printPage;
- (void)showDevTools;
/// Close the browser and its renderer. The view is dead afterwards.
- (void)closeBrowser;
@end

NS_ASSUME_NONNULL_END
