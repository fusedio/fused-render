// FRChromiumEngine: start CEF once, pump its message loop from the main
// run loop, shut it down at quit.
#import "FRChromiumView.h"
#import "FRChromiumInternal.h"

#import <objc/message.h>
#import <objc/runtime.h>

#include <atomic>
#include <string>
#include <vector>

#include "include/cef_application_mac.h"
#include "include/cef_version.h"
#include "include/wrapper/cef_library_loader.h"

#include "FRCefApp.h"

static FRChromiumEngine *g_engine = nil;
static NSString *g_lastError = nil;

// ---- NSApp as a CefAppProtocol ----------------------------------------------
//
// CEF requires the NSApplication to implement CefAppProtocol
// (isHandlingSendEvent / setHandlingSendEvent:) so it can tell when a nested
// run loop sits inside sendEvent:. The app's NSApplication already exists
// (PyObjC / rumps made it) and cannot be subclassed after the fact, so the
// two accessors and the protocol are added to its class at run time and
// sendEvent: is wrapped to maintain the flag — what a CefApplication
// subclass would have done.

static const void *kHandlingSendEventKey = &kHandlingSendEventKey;
static IMP g_originalSendEvent = nullptr;

static BOOL fr_isHandlingSendEvent(id self, SEL _cmd) {
  return [objc_getAssociatedObject(self, kHandlingSendEventKey) boolValue];
}

static void fr_setHandlingSendEvent(id self, SEL _cmd, BOOL value) {
  objc_setAssociatedObject(self, kHandlingSendEventKey, @(value),
                           OBJC_ASSOCIATION_RETAIN_NONATOMIC);
}

static void fr_sendEvent(id self, SEL _cmd, NSEvent *event) {
  BOOL was = fr_isHandlingSendEvent(self, nil);
  fr_setHandlingSendEvent(self, nil, YES);
  if (g_originalSendEvent) {
    ((void (*)(id, SEL, NSEvent *))g_originalSendEvent)(self, _cmd, event);
  }
  fr_setHandlingSendEvent(self, nil, was);
}

static BOOL FRPatchNSApplication(void) {
  Class cls = [NSApp class];
  if (cls == Nil) return NO;
  if ([NSApp conformsToProtocol:@protocol(CefAppProtocol)]) return YES;
  class_addMethod(cls, @selector(isHandlingSendEvent), (IMP)fr_isHandlingSendEvent, "c@:");
  class_addMethod(cls, @selector(setHandlingSendEvent:), (IMP)fr_setHandlingSendEvent, "v@:c");
  Method original = class_getInstanceMethod(cls, @selector(sendEvent:));
  if (original) {
    g_originalSendEvent = method_getImplementation(original);
    // class_addMethod gives this class its own slot when sendEvent: was
    // inherited; class_replaceMethod covers a class that already had one.
    if (!class_addMethod(cls, @selector(sendEvent:), (IMP)fr_sendEvent,
                         method_getTypeEncoding(original))) {
      class_replaceMethod(cls, @selector(sendEvent:), (IMP)fr_sendEvent,
                          method_getTypeEncoding(original));
    }
  }
  class_addProtocol(cls, @protocol(CefAppProtocol));
  return [NSApp conformsToProtocol:@protocol(CefAppProtocol)];
}

// ---- the engine ------------------------------------------------------------------

// Maximum delay between pump runs while work is pending (cefclient's value).
static const int64_t kMaxTimerDelayMs = 1000 / 30;

@implementation FRChromiumEngine {
  CefRefPtr<fused::FRCefApp> _app;
  NSTimer *_pumpTimer;      // the one pending pump run, if any
  BOOL _pumpActive;         // inside CefDoMessageLoopWork (re-entrancy guard)
  BOOL _pumpReentrancyDetected;
  BOOL _shutDown;
  NSMutableArray<NSString *> *_handlerNames;
  NSString *_cefVersion;
  NSHashTable<FRChromiumView *> *_views;
  std::vector<CefRefPtr<CefBrowser>> _auxBrowsers;  // DevTools windows
}

+ (nullable instancetype)shared {
  return g_engine;
}

+ (nullable NSString *)lastError {
  return g_lastError;
}

- (NSString *)cefVersion {
  return _cefVersion;
}

- (NSUInteger)browserCount {
  NSUInteger n = 0;
  for (FRChromiumView *view in _views) {
    if ([view hasBrowser]) n++;
  }
  return n + _auxBrowsers.size();
}

- (NSArray<NSString *> *)scriptMessageHandlerNames {
  return [_handlerNames copy];
}

+ (nullable instancetype)startWithEngineDirectory:(NSString *)engineDir
                                        cachePath:(NSString *)cachePath
                                 userAgentProduct:(NSString *)product
                        scriptMessageHandlerNames:(NSArray<NSString *> *)handlerNames {
  if (g_engine) return g_engine;
  if (![NSThread isMainThread]) {
    g_lastError = @"FRChromiumEngine must start on the main thread";
    return nil;
  }
  NSString *framework = [engineDir stringByAppendingPathComponent:@"Chromium Embedded Framework.framework"];
  NSString *libcef = [framework stringByAppendingPathComponent:@"Chromium Embedded Framework"];
  NSString *helper = [engineDir stringByAppendingPathComponent:
                      @"FusedRender Helper.app/Contents/MacOS/FusedRender Helper"];
  NSFileManager *fm = [NSFileManager defaultManager];
  if (![fm isExecutableFileAtPath:libcef]) {
    g_lastError = [NSString stringWithFormat:@"missing %@", libcef];
    return nil;
  }
  if (![fm isExecutableFileAtPath:helper]) {
    g_lastError = [NSString stringWithFormat:@"missing %@", helper];
    return nil;
  }
  if (!FRPatchNSApplication()) {
    g_lastError = @"could not make NSApplication conform to CefAppProtocol";
    return nil;
  }
  // The framework is loaded by path (not from the app bundle's Frameworks
  // dir, where CefScopedLibraryLoader would look): every CEF call below
  // resolves through this.
  if (!cef_load_library(libcef.fileSystemRepresentation)) {
    g_lastError = [NSString stringWithFormat:@"cef_load_library failed for %@", libcef];
    return nil;
  }

  FRChromiumEngine *engine = [[FRChromiumEngine alloc] init];
  engine->_handlerNames = [handlerNames mutableCopy] ?: [NSMutableArray array];
  engine->_views = [NSHashTable weakObjectsHashTable];
  __weak FRChromiumEngine *weakEngine = engine;
  engine->_app = new fused::FRCefApp([weakEngine](int64_t delayMs) {
    // "Called from any thread": hop to the main queue before touching state.
    dispatch_async(dispatch_get_main_queue(), ^{
      [weakEngine scheduleWorkAfterMilliseconds:delayMs];
    });
  });

  CefSettings settings;
  settings.no_sandbox = true;  // the macOS sandbox needs cef_sandbox.a linked in; v1 runs without
  settings.external_message_pump = true;
  settings.multi_threaded_message_loop = false;
  // This process is Python with its own argv (`-m fused_render.app`, say):
  // none of it is a Chromium switch.
  settings.command_line_args_disabled = true;
  CefString(&settings.framework_dir_path) = framework.fileSystemRepresentation;
  // `main_bundle_path` travels to every helper as --main-bundle-path and
  // becomes its outer bundle; Chromium's AmIBundled() is "outer bundle ends
  // in .app", and a non-bundled host (python -m fused_render.app: the
  // "bundle" is .venv/bin) makes the GPU helper look for libGLESv2.dylib
  // beside its own executable instead of in the framework's Libraries, and
  // exit. Left empty, each helper's own .app is its bundle, which is right.
  NSString *mainBundle = [[NSBundle mainBundle] bundlePath];
  if ([mainBundle hasSuffix:@".app"]) {
    CefString(&settings.main_bundle_path) = mainBundle.fileSystemRepresentation;
  }
  CefString(&settings.browser_subprocess_path) = helper.fileSystemRepresentation;
  CefString(&settings.root_cache_path) = cachePath.fileSystemRepresentation;
  CefString(&settings.cache_path) = cachePath.fileSystemRepresentation;
  // `user_agent_product` REPLACES the Chrome/NN token (it is not appended):
  // keep a Chrome token for pages that sniff it, then our marker.
  NSString *ua = [NSString stringWithFormat:@"Chrome/%d.0.0.0 %@", CHROME_VERSION_MAJOR, product];
  CefString(&settings.user_agent_product) = ua.UTF8String;
  CefString(&settings.log_file) =
      [cachePath stringByAppendingPathComponent:@"cef.log"].fileSystemRepresentation;
  settings.log_severity = LOGSEVERITY_WARNING;
  // Windowed browsers cannot be transparent: an opaque colour matching the
  // window's background keeps the first frame from flashing white.
  settings.background_color = CefColorSetARGB(255, 0x13, 0x14, 0x17);
  settings.persist_session_cookies = true;

  NSArray<NSString *> *argv = [[NSProcessInfo processInfo] arguments];
  std::vector<std::string> args;
  std::vector<char *> argvp;
  for (NSString *a in argv) args.push_back(a.UTF8String);
  for (std::string &a : args) argvp.push_back(&a[0]);
  CefMainArgs mainArgs((int)argvp.size(), argvp.data());
  if (!CefInitialize(mainArgs, settings, engine->_app, nullptr)) {
    g_lastError = [NSString stringWithFormat:@"CefInitialize failed (exit code %d)", CefGetExitCode()];
    return nil;
  }
  engine->_cefVersion = [NSString stringWithFormat:@"%d.%d.%d+chromium-%d.%d.%d.%d",
                         CEF_VERSION_MAJOR, CEF_VERSION_MINOR, CEF_VERSION_PATCH,
                         CHROME_VERSION_MAJOR, CHROME_VERSION_MINOR, CHROME_VERSION_BUILD,
                         CHROME_VERSION_PATCH];
  g_engine = engine;
  g_lastError = nil;
  return engine;
}

// ---- the external message pump (cefclient's MainMessageLoopExternalPump) ----
//
// CEF asks for a pump run through OnScheduleMessagePumpWork(delay); exactly
// one timer is pending at a time, a new request cancels and reschedules it,
// and a request arriving while a run is in progress (a modal panel or a
// context-menu tracking loop spun the run loop under CefDoMessageLoopWork)
// is remembered and honoured right after that run, never nested.

- (void)scheduleWorkAfterMilliseconds:(int64_t)delayMs {
  if (_shutDown) return;
  if (delayMs <= 0) {
    [self performWorkNow];
    return;
  }
  [self setTimerMilliseconds:delayMs];
}

- (void)performWorkNow {
  if (_pumpActive) {
    // Re-entrant request: finish the current run first, then come back.
    _pumpReentrancyDetected = YES;
    return;
  }
  [self cancelTimer];
  _pumpActive = YES;
  _pumpReentrancyDetected = NO;
  CefDoMessageLoopWork();
  _pumpActive = NO;
  if (_pumpReentrancyDetected && !_shutDown) {
    _pumpReentrancyDetected = NO;
    // Soon, on the next turn: not synchronously inside this frame.
    [self setTimerMilliseconds:1];
  }
}

- (void)setTimerMilliseconds:(int64_t)delayMs {
  if (delayMs > kMaxTimerDelayMs) delayMs = kMaxTimerDelayMs;
  [self cancelTimer];
  __weak FRChromiumEngine *weakSelf = self;
  _pumpTimer = [NSTimer scheduledTimerWithTimeInterval:(double)delayMs / 1000.0
                                               repeats:NO
                                                 block:^(NSTimer *t) {
    FRChromiumEngine *me = weakSelf;
    if (!me) return;
    me->_pumpTimer = nil;
    [me performWorkNow];
  }];
  // Common modes: a modal panel or a menu must not starve the pump.
  [[NSRunLoop mainRunLoop] addTimer:_pumpTimer forMode:NSRunLoopCommonModes];
}

- (void)cancelTimer {
  [_pumpTimer invalidate];
  _pumpTimer = nil;
}

// ---- views and auxiliary browsers ----

- (void)registerView:(FRChromiumView *)view {
  [_views addObject:view];
}

- (void)unregisterView:(FRChromiumView *)view {
  [_views removeObject:view];
}

- (void)trackAuxiliaryBrowser:(CefRefPtr<CefBrowser>)browser {
  _auxBrowsers.push_back(browser);
}

- (void)forgetAuxiliaryBrowser:(CefRefPtr<CefBrowser>)browser {
  for (auto it = _auxBrowsers.begin(); it != _auxBrowsers.end(); ++it) {
    if ((*it)->IsSame(browser)) {
      _auxBrowsers.erase(it);
      return;
    }
  }
}

- (void)shutdown {
  if (_shutDown) return;
  // Every browser must be closed before CefShutdown; closing is
  // asynchronous (DoClose → OnBeforeClose), so pump until they are gone,
  // bounded — a quit must not hang on a stuck renderer.
  for (FRChromiumView *view in [_views allObjects]) [view closeBrowser];
  for (auto &b : std::vector<CefRefPtr<CefBrowser>>(_auxBrowsers)) b->GetHost()->CloseBrowser(true);
  NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:3.0];
  while ([self anyBrowserAlive] && [deadline timeIntervalSinceNow] > 0) {
    CefDoMessageLoopWork();
    [[NSRunLoop mainRunLoop] runMode:NSDefaultRunLoopMode
                          beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
  }
  _shutDown = YES;
  [self cancelTimer];
  CefShutdown();
  g_engine = nil;
}

- (BOOL)anyBrowserAlive {
  if (!_auxBrowsers.empty()) return YES;
  for (FRChromiumView *view in _views) {
    if ([view hasBrowser]) return YES;
  }
  return NO;
}

@end
