// Home Screen quick actions (long-press the icon) arrive through UIKit's app
// and scene delegates, not through SwiftUI's onOpenURL. Under the SwiftUI App
// lifecycle the only way to get a scene delegate is to hand its class over
// from the app delegate's configurationForConnecting — a plist scene manifest
// is ignored. The delegates turn a shortcut item back into the deep link it
// carries and hand it to the model: by notification while the app is running,
// and via `pending` when the app is cold-launched from the shortcut (the
// notification would fire before any view is listening).
import UIKit
import os

private let log = Logger(subsystem: "io.fused.render", category: "shortcuts")

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: nil, sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}

final class SceneDelegate: NSObject, UIWindowSceneDelegate {
    static let openNotification = Notification.Name("io.fused.render.openDeepLink")
    /// A deep link that arrived before the UI existed (cold launch).
    static var pending: URL?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
        if let item = options.shortcutItem, let url = Self.url(from: item) {
            log.info("cold launch from quick action: \(url.absoluteString, privacy: .public)")
            Self.pending = url
        }
    }

    func windowScene(_ windowScene: UIWindowScene, performActionFor shortcutItem: UIApplicationShortcutItem,
                     completionHandler: @escaping (Bool) -> Void) {
        guard let url = Self.url(from: shortcutItem) else { completionHandler(false); return }
        log.info("quick action: \(url.absoluteString, privacy: .public)")
        NotificationCenter.default.post(name: Self.openNotification, object: url)
        completionHandler(true)
    }

    private static func url(from item: UIApplicationShortcutItem) -> URL? {
        guard let raw = item.userInfo?["url"] as? String else { return nil }
        return URL(string: raw)
    }
}
