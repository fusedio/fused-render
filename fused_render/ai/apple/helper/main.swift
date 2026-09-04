// fused-apple-ai — the `provider: "apple"` tier's one native process.
//
// Owns the two Apple frameworks Python cannot reach (both are Swift-only, so
// pyobjc has nothing to bind): FoundationModels for `afm-text` and Speech's
// SpeechAnalyzer for `afm-speech`. The server (`fused_render/ai/apple/host.py`)
// spawns ONE of these, keeps it resident, and talks NDJSON over stdin/stdout:
// every request line carries an `id` and an `op`, every reply line echoes the
// `id` and carries a `type`. Requests run concurrently as Tasks; `cancel`
// stops the one it names. Nothing here listens on a port — the helper is a
// child of the server and dies with it.
//
// Wire (one JSON object per line):
//   -> {"id","op":"probe"}
//   <- {"id","type":"probe","available":bool,"reason":str,"os":"26.6.2",
//       "imageInput":false,"speechLocales":[...],"installedLocales":[...]}
//   -> {"id","op":"text","prompt","instructions"?,"history"?:[{role,content}],
//       "temperature"?,"maxTokens"?}
//   <- {"id","type":"chunk","text"}   (DELTAS — the framework's snapshots are
//                                      cumulative; the diff happens here so
//                                      the server relays them untouched)
//   <- {"id","type":"done","ok":true,"finishReason":"stop|length|cancelled|content-filter"}
//   <- {"id","type":"done","ok":false,"error":{"type","message"}}
//   -> {"id","op":"speech","path","locale"?,"words":bool}
//   <- {"id","type":"assets","state":"installing","progress":0..1}  (per tick,
//       only when the locale's model has to be downloaded first)
//   <- {"id","type":"segment","start","end","text","words"?:[{start,end,word}]}
//   <- {"id","type":"done","ok":true,"duration","locale"}
//   -> {"op":"cancel","id":<the id to stop>}
//
// Error `type`s the server maps onto the fused.ai vocabulary: `model_loading`
// (assets still downloading, the caller should wait and retry), `unavailable`
// (device/OS/Apple Intelligence), `bad_request` (unsupported locale, missing
// file), `ai_error` (anything the framework threw mid-run).

import AVFoundation
import Foundation
import FoundationModels
import Speech

// MARK: - stdout, serialised

/// One writer for every Task: two replies interleaving mid-line would corrupt
/// both, and the server's reader cuts on newlines and trusts each one parses.
final class Output: @unchecked Sendable {
    private let lock = NSLock()
    private let handle = FileHandle.standardOutput

    func send(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        lock.lock()
        defer { lock.unlock() }
        handle.write(data)
        handle.write(Data([0x0A]))
    }
}

let out = Output()
let debugging = ProcessInfo.processInfo.environment["FUSED_APPLE_DEBUG"] != nil

func debug(_ message: String) {
    guard debugging else { return }
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

func reply(_ id: String, _ type: String, _ fields: [String: Any] = [:]) {
    var object = fields
    object["id"] = id
    object["type"] = type
    out.send(object)
}

func fail(_ id: String, _ type: String, _ message: String) {
    reply(id, "done", ["ok": false, "error": ["type": type, "message": message]])
}

// MARK: - probe

func osVersionString() -> String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

/// Why the on-device model cannot answer, in the words the server shows.
func availabilityReason(_ model: SystemLanguageModel) -> String? {
    switch model.availability {
    case .available:
        return nil
    case .unavailable(let reason):
        switch reason {
        case .deviceNotEligible:
            return "this Mac cannot run Apple's on-device model (Apple Silicon is required)"
        case .appleIntelligenceNotEnabled:
            return "Apple Intelligence is turned off — enable it in System Settings > Apple Intelligence & Siri"
        case .modelNotReady:
            return "Apple's on-device model is still downloading; try again in a few minutes"
        @unknown default:
            return "Apple's on-device model is unavailable on this Mac"
        }
    }
}

func probe(_ id: String) async {
    let model = SystemLanguageModel.default
    let reason = availabilityReason(model)
    // `modelNotReady` is a WAIT, not a refusal — the server turns it into the
    // same 409 `model_loading` a cold local model answers with.
    var state = "available"
    if reason != nil {
        state = (model.availability == .unavailable(.modelNotReady)) ? "loading" : "unavailable"
    }
    reply(id, "probe", [
        "available": reason == nil,
        "state": state,
        "reason": reason ?? "",
        "os": osVersionString(),
        // The 26.x SDK this is built against has no image-input surface on the
        // session; the day it does, this flag flips and the server's `images`
        // refusal lifts with it. Reported rather than hard-coded server-side so
        // a newer helper on a newer OS can say yes without a Python change.
        "imageInput": false,
        "defaultLocale": Locale.current.identifier(.bcp47),
        "speechLocales": await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) },
        "installedLocales": await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) },
    ])
}

// MARK: - text

/// Prior turns become a `Transcript` the session is opened WITH, so a
/// conversation is rebuilt per call (the server keeps no session state — one
/// process serves every page, and two pages must never share a transcript).
func makeSession(instructions: String?, history: [[String: Any]]) -> LanguageModelSession {
    if history.isEmpty {
        return LanguageModelSession(model: .default, instructions: instructions)
    }
    var entries: [Transcript.Entry] = []
    if let instructions, !instructions.isEmpty {
        entries.append(.instructions(Transcript.Instructions(
            segments: [.text(Transcript.TextSegment(content: instructions))],
            toolDefinitions: [])))
    }
    for turn in history {
        let content = turn["content"] as? String ?? ""
        let segment = Transcript.Segment.text(Transcript.TextSegment(content: content))
        if (turn["role"] as? String) == "assistant" {
            entries.append(.response(Transcript.Response(assetIDs: [], segments: [segment])))
        } else {
            entries.append(.prompt(Transcript.Prompt(segments: [segment])))
        }
    }
    return LanguageModelSession(model: .default, transcript: Transcript(entries: entries))
}

func generateText(_ id: String, _ request: [String: Any]) async {
    if let reason = availabilityReason(.default) {
        let loading = SystemLanguageModel.default.availability == .unavailable(.modelNotReady)
        fail(id, loading ? "model_loading" : "unavailable", reason)
        return
    }
    guard let prompt = request["prompt"] as? String, !prompt.isEmpty else {
        fail(id, "bad_request", "'prompt' must be a non-empty string")
        return
    }
    let history = request["history"] as? [[String: Any]] ?? []
    let session = makeSession(instructions: request["instructions"] as? String, history: history)
    let maxTokens = request["maxTokens"] as? Int
    let options = GenerationOptions(
        temperature: request["temperature"] as? Double,
        maximumResponseTokens: maxTokens)

    var emitted = ""
    var restarted = false
    do {
        let stream = session.streamResponse(to: prompt, options: options)
        for try await snapshot in stream {
            try Task.checkCancellation()
            let whole = snapshot.content
            // Cumulative -> delta. A snapshot that does not extend the last one
            // (the framework re-tokenised and rewrote a suffix) cannot be
            // expressed as an append, so it is sent as a fresh start: the
            // server clears what it buffered and the page sees one `restart`
            // frame it may ignore.
            if whole.hasPrefix(emitted) {
                let delta = String(whole.dropFirst(emitted.count))
                if !delta.isEmpty { reply(id, "chunk", ["text": delta]) }
            } else {
                restarted = true
                reply(id, "restart", [:])
                reply(id, "chunk", ["text": whole])
            }
            emitted = whole
        }
        // `length` when the reply is at least as long as the cap allowed — the
        // framework has no stop-reason field, so hitting the ceiling IS the
        // signal, the same rule `_local_finish_reason` applies to MLX.
        var finish = "stop"
        if let maxTokens, maxTokens > 0 {
            // ~3-4 chars/token for English; a reply that filled the budget is
            // within a few tokens of it, so this is a conservative floor.
            if emitted.count >= maxTokens * 3 { finish = "length" }
        }
        reply(id, "done", ["ok": true, "finishReason": finish, "restarted": restarted,
                           "characters": emitted.count])
    } catch is CancellationError {
        reply(id, "done", ["ok": true, "finishReason": "cancelled", "characters": emitted.count])
    } catch let error as LanguageModelSession.GenerationError {
        switch error {
        case .guardrailViolation, .refusal:
            // The AI SDK's name for "the model declined": the text so far (if
            // any) was already streamed, and the frame says why it stopped.
            reply(id, "done", ["ok": true, "finishReason": "content-filter",
                               "characters": emitted.count,
                               "message": error.localizedDescription])
        case .exceededContextWindowSize:
            fail(id, "ai_error", "the prompt (with history and instructions) exceeds Apple's on-device model's context window (~4096 tokens) — send less")
        case .assetsUnavailable:
            fail(id, "model_loading", "Apple's on-device model assets are not ready yet; try again shortly")
        case .unsupportedLanguageOrLocale:
            fail(id, "bad_request", "Apple's on-device model does not support the language of this prompt")
        case .rateLimited:
            fail(id, "ai_error", "Apple's on-device model is rate-limiting this process; try again shortly")
        case .concurrentRequests:
            fail(id, "ai_error", "Apple's on-device model refused a concurrent request; try again")
        default:
            fail(id, "ai_error", error.localizedDescription)
        }
    } catch {
        fail(id, "ai_error", error.localizedDescription)
    }
}

// MARK: - speech

func seconds(_ time: CMTime) -> Double {
    let s = CMTimeGetSeconds(time)
    return s.isFinite ? (s * 100).rounded() / 100 : 0
}

/// Decode the file's first audio track to PCM buffers in the analyzer's own
/// format. `AVAssetReader`, not `AVAudioFile`: the latter opens audio
/// containers only, and every other transcribe engine here accepts a `.mp4`
/// or `.mov` — a page must not have to know which engine ran (AI-10c).
func pcmStream(url: URL, format: AVAudioFormat) async throws -> (AsyncThrowingStream<AnalyzerInput, Error>, AVAsset) {
    let asset = AVURLAsset(url: url)
    let reader = try AVAssetReader(asset: asset)
    guard let track = try await asset.loadTracks(withMediaType: .audio).first else {
        throw NSError(domain: "fused.apple", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "the file has no audio track"])
    }
    // EXACTLY the analyzer's format — sample rate, channel count AND layout.
    // SpeechAnalyzer traps (SIGTRAP inside `preRunRecognition`) on a buffer
    // whose format merely resembles the one it asked for; an interleaved mono
    // buffer is byte-identical to a planar one and still not accepted.
    let planar = !format.isInterleaved
    let description = format.streamDescription.pointee
    let sampleBytes = Int(description.mBitsPerChannel / 8)
    let isFloat = (description.mFormatFlags & kAudioFormatFlagIsFloat) != 0
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: format.sampleRate,
        AVNumberOfChannelsKey: format.channelCount,
        AVLinearPCMBitDepthKey: sampleBytes * 8,
        AVLinearPCMIsFloatKey: isFloat,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: planar,
    ]
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
    output.alwaysCopiesSampleData = false
    reader.add(output)
    guard reader.startReading() else {
        throw reader.error ?? NSError(domain: "fused.apple", code: 2,
                                      userInfo: [NSLocalizedDescriptionKey: "the audio could not be decoded"])
    }
    let channels = Int(format.channelCount)
    debug("pcm format: \(format) planar=\(planar)")
    let stream = AsyncThrowingStream<AnalyzerInput, Error> { continuation in
        Task.detached {
            var fed = 0
            defer { debug("pcm buffers fed: \(fed)") }
            while let sample = output.copyNextSampleBuffer() {
                fed += 1
                if Task.isCancelled { reader.cancelReading(); break }
                guard let block = CMSampleBufferGetDataBuffer(sample) else { continue }
                let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sample))
                guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { continue }
                buffer.frameLength = frames
                var length = 0
                var pointer: UnsafeMutablePointer<Int8>?
                CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil,
                                            totalLengthOut: &length, dataPointerOut: &pointer)
                if let pointer {
                    // Copy by raw bytes into the buffer's own AudioBufferList,
                    // whatever the sample type: the analyzer asked for Int16
                    // on this machine, and a float copy into an Int16 buffer
                    // is silence with the right length.
                    let list = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
                    let perChannel = Int(frames) * sampleBytes
                    if planar {
                        // Planar block: channel 0's frames, then channel 1's…
                        for c in 0..<min(channels, list.count) where (c + 1) * perChannel <= length {
                            if let dest = list[c].mData { memcpy(dest, pointer + c * perChannel, perChannel) }
                        }
                    } else if let dest = list[0].mData {
                        memcpy(dest, pointer, min(length, perChannel * channels))
                    }
                }
                let start = CMSampleBufferGetPresentationTimeStamp(sample)
                continuation.yield(AnalyzerInput(buffer: buffer, bufferStartTime: start))
            }
            if let error = reader.error { continuation.finish(throwing: error) } else { continuation.finish() }
        }
    }
    return (stream, asset)
}

func transcribe(_ id: String, _ request: [String: Any]) async {
    guard let path = request["path"] as? String, FileManager.default.fileExists(atPath: path) else {
        fail(id, "bad_request", "no such file: \(request["path"] ?? "")")
        return
    }
    let wantsWords = request["words"] as? Bool ?? false
    let asked = (request["locale"] as? String).map { Locale(identifier: $0) } ?? Locale.current
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: asked) else {
        let supported = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }.sorted().joined(separator: ", ")
        fail(id, "bad_request", "Apple's speech model has no \(asked.identifier(.bcp47)) — supported: \(supported)")
        return
    }
    // `.audioTimeRange` is asked for whether or not the caller wants words:
    // the segment's own `range` is what every transcript line needs, and the
    // per-word runs only exist when the attribute is on. `words` decides
    // what is EMITTED below, not what is decoded.
    let transcriber = SpeechTranscriber(
        locale: locale, transcriptionOptions: [],
        reportingOptions: [],
        attributeOptions: [.audioTimeRange])
    do {
        // The locale's model lives in system storage and is fetched on first
        // use. Progress is relayed so the job row shows a download, not a
        // stall; the run continues into the transcription once it lands.
        if let install = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            reply(id, "assets", ["state": "installing", "progress": 0.0])
            let progress = install.progress
            let ticker = Task {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(1))
                    reply(id, "assets", ["state": "installing", "progress": progress.fractionCompleted])
                }
            }
            defer { ticker.cancel() }
            try await install.downloadAndInstall()
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            fail(id, "ai_error", "no audio format is compatible with Apple's speech model")
            return
        }
        let (input, asset) = try await pcmStream(url: URL(fileURLWithPath: path), format: format)
        let duration = seconds(try await asset.load(.duration))
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // Results are consumed on their own Task so segments stream out while
        // the file is still being fed; the analysis call below returns when
        // the input is exhausted.
        let consumer = Task {
            for try await result in transcriber.results {
                try Task.checkCancellation()
                debug("result final=\(result.isFinal) range=\(seconds(result.range.start))-\(seconds(CMTimeAdd(result.range.start, result.range.duration))) text=\(String(result.text.characters).prefix(60))")
                let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                if text.isEmpty { continue }
                var segment: [String: Any] = [
                    "start": seconds(result.range.start),
                    "end": seconds(CMTimeAdd(result.range.start, result.range.duration)),
                    "text": text,
                ]
                if wantsWords {
                    var words: [[String: Any]] = []
                    for run in result.text.runs {
                        guard let range = run.audioTimeRange else { continue }
                        let word = String(result.text[run.range].characters).trimmingCharacters(in: .whitespacesAndNewlines)
                        if word.isEmpty { continue }
                        // `word`, not `text`: the key the Whisper workers write
                        // and `runtime.js`'s `frameSegment` reads.
                        words.append(["start": seconds(range.start),
                                      "end": seconds(CMTimeAdd(range.start, range.duration)),
                                      "word": word])
                    }
                    segment["words"] = words
                }
                reply(id, "segment", segment)
            }
        }
        let last = try await analyzer.analyzeSequence(input)
        debug("analyzeSequence returned last=\(last.map { seconds($0) } ?? -1)")
        if let last {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        debug("finalized")
        try await consumer.value
        reply(id, "done", ["ok": true, "duration": duration, "locale": locale.identifier(.bcp47)])
    } catch is CancellationError {
        reply(id, "done", ["ok": false, "cancelled": true,
                           "error": ["type": "cancelled", "message": "cancelled"]])
    } catch {
        fail(id, "ai_error", error.localizedDescription)
    }
}

// MARK: - dispatcher

/// In-flight requests by id, so `cancel` can reach the Task running one.
actor Registry {
    private var tasks: [String: Task<Void, Never>] = [:]
    func add(_ id: String, _ task: Task<Void, Never>) { tasks[id] = task }
    func remove(_ id: String) { tasks[id] = nil }
    func cancel(_ id: String) { tasks[id]?.cancel() }
    var inFlight: Int { tasks.count }
}

let registry = Registry()

func dispatch(_ line: Data) {
    guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
          let op = object["op"] as? String else {
        out.send(["type": "error", "error": ["type": "bad_request", "message": "unreadable request line"]])
        return
    }
    let id = object["id"] as? String ?? UUID().uuidString
    switch op {
    case "cancel":
        Task { await registry.cancel(id) }
    case "probe", "text", "speech":
        let task = Task {
            switch op {
            case "probe": await probe(id)
            case "text": await generateText(id, object)
            default: await transcribe(id, object)
            }
            await registry.remove(id)
        }
        Task { await registry.add(id, task) }
    default:
        fail(id, "bad_request", "unknown op \(op)")
    }
}

// MARK: - main loop

// One line per request, read until stdin closes — which is how the server
// stops this process: it closes the pipe (or dies), and the loop ends.
setvbuf(stdout, nil, _IONBF, 0)
let stdin = FileHandle.standardInput
var pending = Data()
while true {
    let chunk = stdin.availableData
    if chunk.isEmpty { break }
    pending.append(chunk)
    while let newline = pending.firstIndex(of: 0x0A) {
        let line = pending.subdata(in: pending.startIndex..<newline)
        pending = pending.subdata(in: pending.index(after: newline)..<pending.endIndex)
        if !line.isEmpty { dispatch(line) }
    }
}
// stdin closed: let whatever was already asked finish before exiting, so a
// one-shot caller (`printf ... | fused-apple-ai`) still gets its replies.
let drain = Task {
    while await registry.inFlight > 0 { try? await Task.sleep(for: .milliseconds(50)) }
    exit(0)
}
// Requests dispatched on the last lines may not have been registered yet.
Thread.sleep(forTimeInterval: 0.2)
while true { Thread.sleep(forTimeInterval: 1) }
