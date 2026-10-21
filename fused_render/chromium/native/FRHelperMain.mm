// Entry point of the `FusedRender Helper*.app` bundles: CEF's renderer, GPU,
// utility and plugin processes. LoadInHelper() loads the CEF framework from
// `<helper executable dir>/../../../Chromium Embedded Framework.framework`
// — the engine dir, where the helpers sit beside it — and the process runs
// with the shared app, so the renderer installs the script-message shims.
#include "include/cef_app.h"
#include "include/wrapper/cef_library_loader.h"

#include "FRCefApp.h"

int main(int argc, char* argv[]) {
  CefScopedLibraryLoader library_loader;
  if (!library_loader.LoadInHelper()) return 1;
  CefMainArgs main_args(argc, argv);
  CefRefPtr<fused::FRCefApp> app(new fused::FRCefApp());
  return CefExecuteProcess(main_args, app, nullptr);
}
