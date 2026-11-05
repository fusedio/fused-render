// Fused for iPhone — a thin native shell around fused-render's local-network
// listener (fused_render/lan.py). The computer runs the server; this app pairs
// by scanning the QR code Preferences shows (the code carries the computer's
// local name, its address and the fingerprint of its certificate), and then
// shows the grid, the bots pages and the apps inside a WKWebView. The pairing
// lives in the Keychain, so it is once per computer and survives the Wi-Fi,
// the computer's address and its name changing (AppModel.swift,
// Reachability.swift).
import SwiftUI

@main
struct FusedRenderApp: App {
    // Quick actions come through UIKit delegates (SceneDelegate.swift).
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .preferredColorScheme(nil)
        }
    }
}
