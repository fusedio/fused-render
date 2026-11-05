// Trusting the computer's https listener (fused_render/lan_tls.py) with zero
// user steps. The listener presents ONE self-signed certificate; the pairing
// QR carries its SHA-256 as `fp`; the app accepts a TLS handshake exactly
// when the certificate presented hashes to that — and checks nothing else:
// not the hostname (the computer is reached by its local name OR by its
// address, and the certificate names neither), not a chain (there is none),
// not the expiry. The QR is the ONLY channel that pins a certificate — no
// trust-on-first-use, so nothing on the Wi-Fi can slip its own in ahead of a
// pairing.
import CryptoKit
import Foundation
import Security
import os

private let log = Logger(subsystem: "io.fused.render", category: "tls")

enum TLSTrust {
    /// Does the chain `trust` presents start with the certificate whose
    /// SHA-256 (hex) is `fingerprint`?
    static func accepts(_ trust: SecTrust, fingerprint: String) -> Bool {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else { return false }
        let der = SecCertificateCopyData(leaf) as Data
        let got = Self.fingerprint(der)
        if got != fingerprint.lowercased() {
            log.info("tls: rejected a certificate (\(got, privacy: .public))")
            return false
        }
        return true
    }

    static func fingerprint(_ der: Data) -> String {
        SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
    }
}

/// A URLSession that trusts one computer's certificate (and nothing else for
/// hosts presenting it), for the native pieces that talk to the server outside
/// the webview — the capture bridge's uploads, the manifest refresh and the
/// reachability probe. The delegate is a separate object: a URLSession retains
/// its delegate, so a session whose delegate is the owner never lets the
/// owner go. This way the only cycle is session→delegate, and deinit's
/// invalidate breaks it.
final class PinnedSession {
    private let session: URLSession

    init(fingerprint: String?) {
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        session = URLSession(configuration: config,
                             delegate: PinDelegate(fingerprint: fingerprint),
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
    private let fingerprint: String?

    init(fingerprint: String?) {
        self.fingerprint = fingerprint
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        if let fingerprint, TLSTrust.accepts(trust, fingerprint: fingerprint) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}
