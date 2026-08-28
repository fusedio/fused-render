// Trusting the computer's private CA (fused_render/lan_tls.py) with zero user
// steps. The pairing QR carries the CA's SHA-256 fingerprint; the app fetches
// /lan/ca.pem over http, checks it against that fingerprint, and from then on
// evaluates the https listener's chain against THAT certificate only — no
// profile install, no Settings trip. A computer found over Bonjour (no QR) is
// trusted on first use: its /api/lan/tls names the fingerprint, and the first
// contact is when the pairing code — the real credential — is exchanged anyway.
import CryptoKit
import Foundation
import Security
import os

private let log = Logger(subsystem: "io.fused.render", category: "tls")

enum TLSTrust {
    /// Does `trust` (the server's presented chain) lead to `caDER`, and is it
    /// valid for `host`? Standard evaluation with our CA as the only anchor.
    static func accepts(_ trust: SecTrust, caDER: Data, host: String) -> Bool {
        guard let anchor = SecCertificateCreateWithData(nil, caDER as CFData) else { return false }
        SecTrustSetAnchorCertificates(trust, [anchor] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString))
        var error: CFError?
        let ok = SecTrustEvaluateWithError(trust, &error)
        if !ok { log.info("tls: rejected for \(host, privacy: .public): \(String(describing: error), privacy: .public)") }
        return ok
    }

    static func fingerprint(_ der: Data) -> String {
        SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
    }

    /// Fetch the CA over plain http and verify it against `expected` (hex
    /// SHA-256 of the DER). Returns the DER when it matches.
    static func fetchCA(host: String, httpPort: Int, expected: String) async -> Data? {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = httpPort == 80 ? nil : httpPort
        c.path = "/lan/ca.pem"
        guard let url = c.url else { return nil }
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let der = derFromPEM(data) else { return nil }
        let got = fingerprint(der)
        guard got == expected.lowercased() else {
            log.error("tls: CA fingerprint mismatch for \(host, privacy: .public): got \(got, privacy: .public)")
            return nil
        }
        return der
    }

    /// The computer's own answer to "is there https, and which CA?" — for a
    /// server found over Bonjour. Returns (httpsPort, fingerprint).
    static func probeTLS(host: String, httpPort: Int) async -> (Int, String)? {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = httpPort == 80 ? nil : httpPort
        c.path = "/api/lan/tls"
        guard let url = c.url else { return nil }
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 4
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = obj["https_port"] as? Int,
              let fp = obj["ca_fingerprint"] as? String else { return nil }
        return (port, fp)
    }

    static func derFromPEM(_ pem: Data) -> Data? {
        guard let text = String(data: pem, encoding: .utf8) else { return nil }
        // First certificate block only (the CA file has exactly one).
        guard let start = text.range(of: "-----BEGIN CERTIFICATE-----"),
              let end = text.range(of: "-----END CERTIFICATE-----", range: start.upperBound..<text.endIndex) else { return nil }
        let body = text[start.upperBound..<end.lowerBound].filter { !$0.isWhitespace }
        return Data(base64Encoded: String(body))
    }
}

/// A URLSession that trusts one server's private CA (and nothing else for
/// https), for the native pieces that talk to the server outside the webview —
/// the capture bridge's uploads and the manifest refresh.
final class PinnedSession: NSObject, URLSessionDelegate {
    private let caDER: Data?
    let session: URLSession

    init(caDER: Data?) {
        self.caDER = caDER
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
        super.init()
        // Re-create with self as delegate (delegate must exist at init).
        self.sessionWithDelegate = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    private var sessionWithDelegate: URLSession!

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await sessionWithDelegate.data(for: request)
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        if let ca = caDER, TLSTrust.accepts(trust, caDER: ca, host: challenge.protectionSpace.host) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}
