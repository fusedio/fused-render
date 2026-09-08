// Trusting the computer's private CA (fused_render/lan_tls.py) with zero user
// steps. The pairing QR carries the CA's SHA-256 fingerprint; the app fetches
// /lan/ca.pem (https, unvalidated — the fingerprint is the trust, not the
// transport — falling back to http), checks it against that fingerprint, and
// evaluates the https listener's chain against THAT certificate only — no
// profile install, no Settings trip. The QR is the ONLY channel that pins a
// CA — no trust-on-first-use from Bonjour, so nothing on the Wi-Fi can slip
// its own certificate in ahead of a pairing.
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

    /// Why fetching the CA failed. The three are worth telling apart: only
    /// `mismatch` means a certificate that is not the one the pairing code
    /// named — the other two are "we never got a certificate at all", and
    /// reporting those as a mismatch sent people hunting for a fresh code that
    /// could not help.
    enum CAProblem: Error {
        /// Nothing answered on either port — the phone is on another network,
        /// Local Network access is denied, or the listener is down.
        case unreachable
        /// Something answered, but not with a certificate.
        case badResponse
        /// A certificate that is not the one the pairing code named.
        case mismatch
    }

    /// Fetch the CA and verify it against `expected` (hex SHA-256 of the DER).
    ///
    /// https FIRST, over a session that accepts whatever certificate answers:
    /// the transport is not the trust here — the fingerprint the QR carried is,
    /// and it is checked against the bytes that come back — so an unvalidated
    /// fetch plus that check is exactly as strong as the plain-http fetch this
    /// replaces. It also keeps pairing working when the computer's http
    /// listener is down, which the https one the pairing itself runs over does
    /// not depend on. http stays as the fallback.
    static func fetchCA(host: String, httpPort: Int, httpsPort: Int?,
                        expected: String) async -> Result<Data, CAProblem> {
        var problem = CAProblem.unreachable
        var attempts: [(String, Int)] = []
        if let httpsPort { attempts.append(("https", httpsPort)) }
        attempts.append(("http", httpPort))
        for (scheme, port) in attempts {
            switch await fetchCA(scheme: scheme, host: host, port: port, expected: expected) {
            case .success(let der):
                return .success(der)
            case .failure(let kind):
                // A mismatch is the answer: a computer answering to this name
                // holds a CA that is not the one in the code, and the other
                // port will not change that. Otherwise keep the most specific
                // problem seen so far.
                if kind == .mismatch { return .failure(.mismatch) }
                if kind == .badResponse { problem = .badResponse }
            }
        }
        return .failure(problem)
    }

    private static func fetchCA(scheme: String, host: String, port: Int,
                                expected: String) async -> Result<Data, CAProblem> {
        var c = URLComponents()
        c.scheme = scheme
        c.host = host
        c.port = port == (scheme == "https" ? 443 : 80) ? nil : port
        c.path = "/lan/ca.pem"
        guard let url = c.url else { return .failure(.unreachable) }
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 5
        let session = PinnedSession(caDER: nil, acceptAnyCertificate: scheme == "https")
        guard let (data, response) = try? await session.data(for: req) else {
            log.info("tls: CA fetch over \(scheme, privacy: .public) did not answer for \(host, privacy: .public)")
            return .failure(.unreachable)
        }
        guard (response as? HTTPURLResponse)?.statusCode == 200, let der = derFromPEM(data) else {
            log.error("tls: CA fetch over \(scheme, privacy: .public) answered without a certificate for \(host, privacy: .public)")
            return .failure(.badResponse)
        }
        let got = fingerprint(der)
        guard got == expected.lowercased() else {
            log.error("tls: CA fingerprint mismatch for \(host, privacy: .public): got \(got, privacy: .public)")
            return .failure(.mismatch)
        }
        return .success(der)
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
/// the capture bridge's uploads and the manifest refresh. The delegate is a
/// separate object: a URLSession retains its delegate, so a session whose
/// delegate is the owner never lets the owner go. This way the only cycle is
/// session→delegate, and deinit's invalidate breaks it.
final class PinnedSession {
    private let session: URLSession

    /// `acceptAnyCertificate` is for the ONE request that has no anchor yet —
    /// fetching the CA a pairing code named, where the code's fingerprint is
    /// the trust and is checked on the bytes that come back (`fetchCA`).
    /// Everything else passes the pinned CA and nothing else is trusted.
    init(caDER: Data?, acceptAnyCertificate: Bool = false) {
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        session = URLSession(configuration: config,
                             delegate: PinDelegate(caDER: caDER, acceptAny: acceptAnyCertificate),
                             delegateQueue: nil)
    }

    deinit {
        session.finishTasksAndInvalidate()
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
}

private final class PinDelegate: NSObject, URLSessionDelegate {
    private let caDER: Data?
    private let acceptAny: Bool

    init(caDER: Data?, acceptAny: Bool = false) {
        self.caDER = caDER
        self.acceptAny = acceptAny
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        if acceptAny {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else if let ca = caDER, TLSTrust.accepts(trust, caDER: ca, host: challenge.protectionSpace.host) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}
