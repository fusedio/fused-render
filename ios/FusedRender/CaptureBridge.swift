// Native side of `fused.capture.*` for pages in the shell (Resources/runtime-ios.js
// is the JS side). A page on plain http cannot use the microphone or take a
// picture of itself; the app can. Each call arrives as {id, op, args, page},
// runs natively, uploads its file to the server the page came from, and
// answers with the desktop's own result shapes so apps run unchanged.
//
// Uploads go through /api/fs/upload with the WEBVIEW'S cookie (read from the
// shared WKHTTPCookieStore) — the same pairing the page holds, so nothing new
// to store and nothing the server has to learn. Files land beside the app
// (`<app dir>/captures/…`) unless the call names a `path`: the listener only
// writes inside the app roots (lan.py allowed_roots), so a laptop-style
// default such as ~/recordings would be refused.
import AVFoundation
import Foundation
import ReplayKit
import UIKit
import WebKit
import os

private let log = Logger(subsystem: "io.fused.render", category: "capture")

@MainActor
final class CaptureBridge: NSObject, WKScriptMessageHandler {
    static let handlerName = "fusedCapture"

    weak var webView: WKWebView?

    private var audio: AudioRecording?
    private var screen: ScreenRecording?

    // MARK: messages

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let id = body["id"] as? String,
              let op = body["op"] as? String else { return }
        let args = body["args"] as? [String: Any] ?? [:]
        let page = body["page"] as? String
        Task { @MainActor in
            do {
                let value = try await dispatch(op: op, args: args, page: page)
                reply(id: id, value: value)
            } catch let error as BridgeError {
                fail(id: id, message: error.message, type: error.type)
            } catch {
                fail(id: id, message: error.localizedDescription, type: "capture_error")
            }
        }
    }

    private func dispatch(op: String, args: [String: Any], page: String?) async throws -> Any {
        switch op {
        case "sources":
            return sources()
        case "list":
            return [] as [Any]
        case "audio":
            return try await startAudio(args: args, page: page)
        case "screen":
            return try await startScreen(args: args, page: page)
        case "stop", "cancel":
            return try await end(id: args["id"] as? String ?? "", cancel: op == "cancel")
        case "screenshot":
            return try await screenshot(args: args, page: page)
        default:
            throw BridgeError("unknown capture call: \(op)")
        }
    }

    private func reply(id: String, value: Any) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed]),
              let json = String(data: data, encoding: .utf8) else {
            fail(id: id, message: "result was not serialisable", type: "capture_error")
            return
        }
        webView?.evaluateJavaScript("window.__fusedBridge && window.__fusedBridge.resolve(\(jsString(id)), \(json))")
    }

    private func fail(id: String, message: String, type: String) {
        let err = ["message": message, "type": type]
        let json = String(data: try! JSONSerialization.data(withJSONObject: err), encoding: .utf8)!
        webView?.evaluateJavaScript("window.__fusedBridge && window.__fusedBridge.reject(\(jsString(id)), \(json))")
    }

    // MARK: sources — the server's dict shape, answered for this device

    private func sources() -> [String: Any] {
        let session = AVAudioSession.sharedInstance()
        let micGranted = session.recordPermission == .granted
        let mics: [[String: Any]] = (session.availableInputs ?? []).map {
            ["id": $0.uid, "name": $0.portName, "default": $0.uid == session.currentRoute.inputs.first?.uid]
        }
        let screenSize = UIScreen.main.bounds.size
        return [
            "device": "ios-app",
            "native": true,
            "displays": [["id": "phone", "name": "This phone", "width": Int(screenSize.width), "height": Int(screenSize.height), "primary": true]],
            "microphones": mics,
            "video": ["available": RPScreenRecorder.shared().isAvailable, "granted": true, "reason": NSNull()],
            "audio": ["available": true, "granted": micGranted,
                      "reason": (micGranted ? NSNull() : "the microphone permission has not been granted yet; the first recording asks") as Any],
            "systemAudio": ["available": false, "reason": "iOS lets an app record its own audio only through a screen recording"],
            "screenshot": ["available": true, "kind": "page"],
        ]
    }

    // MARK: audio

    private func startAudio(args: [String: Any], page: String?) async throws -> [String: Any] {
        if audio != nil || screen != nil { throw BridgeError("a recording is already running; stop it first") }
        guard await requestMic() else {
            throw BridgeError("microphone access was not granted", type: "permission_denied")
        }
        let path = (args["path"] as? String) ?? Self.defaultPath(kind: "audio", ext: "m4a", page: page)
        let maxSeconds = (args["maxSeconds"] as? Double)
        let rec = try AudioRecording(maxSeconds: maxSeconds)
        rec.targetPath = path
        audio = rec
        rec.onAutoStop = { [weak self] in Task { @MainActor in _ = try? await self?.end(id: rec.id, cancel: false) } }
        try rec.start()
        return ["id": rec.id, "mode": "audio", "path": path, "maxSeconds": maxSeconds.map { $0 as Any } ?? NSNull(), "jobId": NSNull()]
    }

    private func requestMic() async -> Bool {
        let session = AVAudioSession.sharedInstance()
        switch session.recordPermission {
        case .granted: return true
        case .denied: return false
        default:
            return await withCheckedContinuation { c in session.requestRecordPermission { c.resume(returning: $0) } }
        }
    }

    // MARK: screen (ReplayKit — this app's own screen)

    private func startScreen(args: [String: Any], page: String?) async throws -> [String: Any] {
        if audio != nil || screen != nil { throw BridgeError("a recording is already running; stop it first") }
        let recorder = RPScreenRecorder.shared()
        guard recorder.isAvailable else { throw BridgeError("screen recording is not available on this device", type: "unavailable") }
        let path = (args["path"] as? String) ?? Self.defaultPath(kind: "screen", ext: "mp4", page: page)
        let wantsMic: Bool = {
            if let a = args["audio"] as? String { return a == "mic" || a == "both" }
            return false
        }()
        if wantsMic, !(await requestMic()) {
            throw BridgeError("microphone access was not granted", type: "permission_denied")
        }
        recorder.isMicrophoneEnabled = wantsMic
        let rec = ScreenRecording(targetPath: path, maxSeconds: args["maxSeconds"] as? Double)
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            recorder.startRecording { error in
                if let error { c.resume(throwing: BridgeError(error.localizedDescription)) } else { c.resume() }
            }
        }
        screen = rec
        rec.onAutoStop = { [weak self] in Task { @MainActor in _ = try? await self?.end(id: rec.id, cancel: false) } }
        rec.armTimer()
        return ["id": rec.id, "mode": "screen", "path": path, "maxSeconds": rec.maxSeconds.map { $0 as Any } ?? NSNull(), "jobId": NSNull()]
    }

    // MARK: stop / cancel

    private func end(id: String, cancel: Bool) async throws -> [String: Any] {
        if let rec = audio, rec.id == id {
            audio = nil
            let file = rec.stop()
            if cancel { try? FileManager.default.removeItem(at: file); return ["state": "cancelled"] }
            return try await upload(file: file, to: rec.targetPath, mime: "audio/mp4", state: "stopped", extra: ["seconds": rec.seconds])
        }
        if let rec = screen, rec.id == id {
            screen = nil
            rec.disarm()
            let file = try await rec.stop()
            if cancel { try? FileManager.default.removeItem(at: file); return ["state": "cancelled"] }
            return try await upload(file: file, to: rec.targetPath, mime: "video/mp4", state: "stopped", extra: ["seconds": rec.seconds])
        }
        throw BridgeError("no recording with that id is running", type: "not_found")
    }

    // MARK: screenshot — a picture of the page itself

    private func screenshot(args: [String: Any], page: String?) async throws -> [String: Any] {
        guard let webView else { throw BridgeError("no page to capture") }
        let path = (args["path"] as? String) ?? Self.defaultPath(kind: "screenshot", ext: "png", page: page)
        let image: UIImage = try await withCheckedThrowingContinuation { c in
            webView.takeSnapshot(with: nil) { image, error in
                if let image { c.resume(returning: image) } else { c.resume(throwing: BridgeError(error?.localizedDescription ?? "snapshot failed")) }
            }
        }
        guard let png = image.pngData() else { throw BridgeError("could not encode the snapshot") }
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        try png.write(to: tmp)
        var result = try await upload(file: tmp, to: path, mime: "image/png", state: nil, extra: [:])
        result["width"] = Int(image.size.width * image.scale)
        result["height"] = Int(image.size.height * image.scale)
        return result
    }

    // MARK: upload — /api/fs/upload with the page's own cookie

    private func upload(file: URL, to path: String, mime: String, state: String?, extra: [String: Any]) async throws -> [String: Any] {
        defer { try? FileManager.default.removeItem(at: file) }
        guard let webView, let pageURL = webView.url, var comps = URLComponents(url: pageURL, resolvingAgainstBaseURL: false) else {
            throw BridgeError("no server to upload to")
        }
        comps.path = "/api/fs/upload"
        comps.query = nil
        comps.fragment = nil
        guard let endpoint = comps.url else { throw BridgeError("bad server address") }

        let cookies = await webView.configuration.websiteDataStore.httpCookieStore.allCookies()
        let cookieHeader = cookies
            .filter { c in pageURL.host.map { $0.hasSuffix(c.domain.trimmingCharacters(in: CharacterSet(charactersIn: "."))) } ?? false }
            .map { "\($0.name)=\($0.value)" }
            .joined(separator: "; ")

        let boundary = "fused-" + UUID().uuidString
        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".data(using: .utf8)!)
        }
        field("path", path)
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(file.lastPathComponent)\"\r\nContent-Type: \(mime)\r\n\r\n".data(using: .utf8)!)
        body.append(try Data(contentsOf: file))
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Fused")
        if !cookieHeader.isEmpty { req.setValue(cookieHeader, forHTTPHeaderField: "Cookie") }
        req.setValue(WebView.userAgentMarker, forHTTPHeaderField: "User-Agent")
        req.httpBody = body

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw BridgeError("no response from the server") }
        guard (200..<300).contains(http.statusCode) else {
            let text = String(data: data, encoding: .utf8) ?? ""
            throw BridgeError("upload failed (HTTP \(http.statusCode)): \(text.prefix(200))", type: http.statusCode == 401 ? "not_paired" : "capture_error")
        }
        let bytes = (try? FileManager.default.attributesOfItem(atPath: file.path)[.size] as? Int) ?? body.count
        var result: [String: Any] = [
            "path": path,
            "url": "/api/fs/raw?path=" + (path.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? path),
            "bytes": bytes,
            "mime": mime,
            "device": "ios-app",
        ]
        if let state { result["state"] = state }
        for (k, v) in extra { result[k] = v }
        log.info("uploaded \(path, privacy: .public) (\(bytes) bytes)")
        return result
    }

    // MARK: paths

    /// `<app dir>/captures/<kind>-<stamp>.<ext>` beside the page; the listener
    /// writes only inside the app roots.
    static func defaultPath(kind: String, ext: String, page: String?) -> String {
        let dir = (page as NSString?)?.deletingLastPathComponent ?? ""
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        return "\(dir)/captures/\(kind)-\(f.string(from: Date())).\(ext)"
    }
}

// MARK: - recordings

final class AudioRecording: NSObject, AVAudioRecorderDelegate {
    let id = "ios-audio-" + UUID().uuidString.prefix(8)
    let maxSeconds: Double?
    var targetPath = ""
    var onAutoStop: (() -> Void)?
    private let recorder: AVAudioRecorder
    private let file: URL
    private var started = Date()
    private var timer: Timer?
    private(set) var seconds: Double = 0

    init(maxSeconds: Double?) throws {
        self.maxSeconds = maxSeconds
        file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".m4a")
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetooth])
        try session.setActive(true)
        recorder = try AVAudioRecorder(url: file, settings: [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ])
        super.init()
        recorder.delegate = self
    }

    func start() throws {
        guard recorder.record() else { throw BridgeError("the microphone did not start") }
        started = Date()
        if let max = maxSeconds, max > 0 {
            timer = Timer.scheduledTimer(withTimeInterval: max, repeats: false) { [weak self] _ in self?.onAutoStop?() }
        }
    }

    func stop() -> URL {
        timer?.invalidate()
        seconds = Date().timeIntervalSince(started)
        recorder.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        return file
    }
}

final class ScreenRecording {
    let id = "ios-screen-" + UUID().uuidString.prefix(8)
    let targetPath: String
    let maxSeconds: Double?
    var onAutoStop: (() -> Void)?
    private var started = Date()
    private var timer: Timer?
    private(set) var seconds: Double = 0

    init(targetPath: String, maxSeconds: Double?) {
        self.targetPath = targetPath
        self.maxSeconds = maxSeconds
    }

    func armTimer() {
        started = Date()
        if let max = maxSeconds, max > 0 {
            timer = Timer.scheduledTimer(withTimeInterval: max, repeats: false) { [weak self] _ in self?.onAutoStop?() }
        }
    }

    func disarm() { timer?.invalidate() }

    func stop() async throws -> URL {
        seconds = Date().timeIntervalSince(started)
        let out = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            RPScreenRecorder.shared().stopRecording(withOutput: out) { error in
                if let error { c.resume(throwing: BridgeError(error.localizedDescription)) } else { c.resume() }
            }
        }
        return out
    }
}

struct BridgeError: Error {
    let message: String
    let type: String
    init(_ message: String, type: String = "capture_error") {
        self.message = message
        self.type = type
    }
}

private func jsString(_ s: String) -> String {
    let data = try! JSONSerialization.data(withJSONObject: [s])
    let arr = String(data: data, encoding: .utf8)!
    return String(arr.dropFirst().dropLast())
}
