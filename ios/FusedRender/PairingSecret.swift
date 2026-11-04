// The pairing itself, kept by the app rather than only by WebKit's cookie
// jar. /pair sets the `fused_lan_device` cookie for the host the pairing ran
// on; cookies are keyed on the host, so a computer reached later by its
// address instead of its name (a network filtering mDNS), or renamed, would
// look unpaired to WebKit although the computer still knows the phone. So
// once the grid lands after /pair, the cookie's value is copied into the
// Keychain, keyed on the computer's certificate fingerprint (its identity),
// and set again for whatever host the webview is about to load. The server
// only ever checks the hash of the value (lan.py `_paired_record`), never
// the host it arrived under.
import Foundation
import Security
import WebKit

enum PairingSecret {
    static let cookieName = "fused_lan_device"
    private static let service = "io.fused.render.pairing"

    static func get(for server: Server) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.fingerprint,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ secret: String, for server: Server) {
        delete(for: server)
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.fingerprint,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: Data(secret.utf8),
        ]
        SecItemAdd(add as CFDictionary, nil)
    }

    static func delete(for server: Server) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.fingerprint,
        ]
        SecItemDelete(query as CFDictionary)
    }

    /// The `Cookie:` header value for a native request to this computer, or
    /// nil when the phone has not paired with it.
    static func cookieHeader(for server: Server) -> String? {
        get(for: server).map { "\(cookieName)=\($0)" }
    }

    /// After /pair landed on the grid: take the cookie WebKit holds for the
    /// host the pairing ran on and keep it.
    @MainActor
    static func capture(from webView: WKWebView, server: Server, host: String) async {
        let cookies = await webView.configuration.websiteDataStore.httpCookieStore.allCookies()
        guard let cookie = cookies.first(where: {
            $0.name == cookieName && $0.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")) == host
        }) else { return }
        set(cookie.value, for: server)
    }

    /// Before loading a page from this computer on `host`: make sure WebKit
    /// carries the pairing for that host. A no-op when nothing is stored (the
    /// first pairing) — /pair then sets the cookie itself.
    @MainActor
    static func install(into webView: WKWebView, server: Server, host: String) async {
        guard let secret = get(for: server) else { return }
        let store = webView.configuration.websiteDataStore.httpCookieStore
        let existing = await store.allCookies()
        if existing.contains(where: { $0.name == cookieName && $0.value == secret
                                && $0.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")) == host }) {
            return
        }
        guard let cookie = HTTPCookie(properties: [
            .domain: host,
            .path: "/",
            .name: cookieName,
            .value: secret,
            .secure: "TRUE",
            .expires: Date(timeIntervalSinceNow: 90 * 86400),
        ]) else { return }
        await store.setCookie(cookie)
    }
}
