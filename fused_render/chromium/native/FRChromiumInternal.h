// Framework-private surface between the engine and the view.
#import "FRChromiumView.h"

#include "include/cef_browser.h"

NS_ASSUME_NONNULL_BEGIN

@interface FRChromiumEngine ()
- (NSArray<NSString *> *)scriptMessageHandlerNames;
- (void)registerView:(FRChromiumView *)view;
- (void)unregisterView:(FRChromiumView *)view;
- (void)scheduleWorkAfterMilliseconds:(int64_t)delayMs;
/// A browser CEF made its own window for (DevTools): closed at shutdown.
- (void)trackAuxiliaryBrowser:(CefRefPtr<CefBrowser>)browser;
- (void)forgetAuxiliaryBrowser:(CefRefPtr<CefBrowser>)browser;
@end

@interface FRChromiumView ()
- (BOOL)hasBrowser;
@end

NS_ASSUME_NONNULL_END
