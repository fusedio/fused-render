// Fused for iPhone — a thin native shell around fused-render's local-network
// listener (fused_render/lan.py). The computer runs the server; this app finds
// it over Bonjour, pairs by scanning the QR code Preferences shows, and then
// shows the same grid and apps a phone browser would, inside a WKWebView whose
// cookie jar persists — so pairing is once, not once per browser.
//
// v1 is deliberately small: discover → pair → grid → apps. The native bridge
// for capture (microphone, snapshots) and per-app icons are the next steps and
// are designed for (see WebView.swift's user-agent marker), not built here.
import SwiftUI

@main
struct FusedRenderApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .preferredColorScheme(nil)
        }
    }
}
