// fused-apple-ai — the `provider: "apple"` tier's native process, ONE request
// per process.
//
// Owns the two Apple frameworks Python cannot reach (both are Swift-only, so
// pyobjc has nothing to bind): FoundationModels for `afm-text` and Speech's
// SpeechAnalyzer for `afm-speech`. The server (`fused_render/ai/apple/host.py`)
// spawns one of these per call, feeds ONE JSON object on stdin, reads NDJSON
// frames off stdout until the process exits, and cancels by terminating it.
// Nothing here listens, multiplexes or stays resident: the weights live in
// the OS's own daemon (modelmanagerd / the speech assets), so a fresh process
// per request costs a spawn and nothing else — measured ~1 s for a whole cold
// text call, and a transcription is a job anyway.
//
//   fused-apple-ai probe
//     <- {"type":"probe","available","state","reason","os","imageInput",
//         "defaultLocale","speechLocales":[...],"installedLocales":[...]}
//   fused-apple-ai text   <<< {"prompt","instructions"?,"history"?:[{role,content}],
//                              "temperature"?,"maxTokens"?}
//     <- {"type":"chunk","text"}   (DELTAS — the framework's snapshots are
//                                   cumulative; the diff happens here)
//     <- {"type":"done","ok":true,"finishReason":"stop|length|content-filter","characters"}
//     <- {"type":"done","ok":false,"error":{"type","message"}}
//   fused-apple-ai speech <<< {"path","locale"?,"words":bool}
//     <- {"type":"assets","state":"installing"}   (once, only when the locale's
//                                                  model has to download first)
//     <- {"type":"segment","start","end","text","words"?:[{start,end,word}]}
//     <- {"type":"done","ok":true,"duration","locale"}
//
// Error `type`s the server maps onto the fused.ai vocabulary: `model_loading`
// (the OS is still fetching the model — wait and retry), `unavailable`
// (device/OS/Apple Intelligence), `bad_request` (unsupported locale, missing
// file), `ai_error` (anything the framework threw mid-run).

import AVFoundation
import Foundation
import FoundationModels
import Speech

// MARK: - stdout

func send(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func frame(_ type: String, _ fields: [String: Any] = [:]) {
    var object = fields
    object["type"] = type
    send(object)
}

func fail(_ type: String, _ message: String) -> Never {
    frame("done", ["ok": false, "error": ["type": type, "message": message]])
    exit(0)
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

func isLoading(_ model: SystemLanguageModel) -> Bool {
    model.availability == .unavailable(.modelNotReady)
}

func probe() async {
    let model = SystemLanguageModel.default
    let reason = availabilityReason(model)
    frame("probe", [
        "available": reason == nil,
        // `modelNotReady` is a WAIT, not a refusal — the server turns it into
        // the same 409 `model_loading` a cold local model answers with.
        "state": reason == nil ? "available" : (isLoading(model) ? "loading" : "unavailable"),
        "reason": reason ?? "",
        "os": osVersionString(),
        // The 26.x SDK this is built against has no image-input surface on the
        // session; the day it does, this flag flips and the server's `images`
        // refusal lifts with it.
        "imageInput": false,
        // The USER's language, not `Locale.current`: a child of the server
        // inherits no LANG, so `current` degrades to a region-less "en" and
        // the server's locale mapping then has no region to match. The
        // preferred-languages list reads the user's own setting.
        "defaultLocale": Locale.preferredLanguages.first ?? Locale.current.identifier(.bcp47),
        "speechLocales": await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) },
        "installedLocales": await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) },
    ])
}

// MARK: - text

/// Prior turns become a `Transcript` the session is opened WITH, so a
/// conversation is rebuilt per call — one process, one request, no state.
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
        let segment = Transcript.Segment.text(Transcript.TextSegment(content: turn["content"] as? String ?? ""))
        if (turn["role"] as? String) == "assistant" {
            entries.append(.response(Transcript.Response(assetIDs: [], segments: [segment])))
        } else {
            entries.append(.prompt(Transcript.Prompt(segments: [segment])))
        }
    }
    return LanguageModelSession(model: .default, transcript: Transcript(entries: entries))
}

func generateText(_ request: [String: Any]) async {
    if let reason = availabilityReason(.default) {
        fail(isLoading(.default) ? "model_loading" : "unavailable", reason)
    }
    guard let prompt = request["prompt"] as? String, !prompt.isEmpty else {
        fail("bad_request", "'prompt' must be a non-empty string")
    }
    let session = makeSession(instructions: request["instructions"] as? String,
                              history: request["history"] as? [[String: Any]] ?? [])
    let maxTokens = request["maxTokens"] as? Int
    let options = GenerationOptions(temperature: request["temperature"] as? Double,
                                    maximumResponseTokens: maxTokens)
    var emitted = ""
    do {
        for try await snapshot in session.streamResponse(to: prompt, options: options) {
            let whole = snapshot.content
            // Cumulative -> delta. The framework has only ever extended the
            // previous snapshot; if it ever rewrites one instead, the whole
            // text goes out again as one chunk and `done` says so — the server
            // rebuilds `text` from the frames, so the result stays right even
            // where a page's own concatenation would not.
            if whole.hasPrefix(emitted) {
                let delta = String(whole.dropFirst(emitted.count))
                if !delta.isEmpty { frame("chunk", ["text": delta]) }
            } else {
                frame("chunk", ["text": whole])
            }
            emitted = whole
        }
        // `length` when the reply is at least as long as the cap allowed — the
        // framework has no stop-reason field, so hitting the ceiling IS the
        // signal (the rule `_local_finish_reason` applies to MLX). ~3-4 chars
        // per English token makes this a conservative floor.
        let capped = (maxTokens ?? 0) > 0 && emitted.count >= (maxTokens ?? 0) * 3
        frame("done", ["ok": true, "finishReason": capped ? "length" : "stop", "characters": emitted.count])
    } catch let error as LanguageModelSession.GenerationError {
        switch error {
        case .guardrailViolation, .refusal:
            // The AI SDK's name for "the model declined": the text so far was
            // already streamed, and the frame says why it stopped.
            frame("done", ["ok": true, "finishReason": "content-filter",
                           "characters": emitted.count, "message": error.localizedDescription])
        case .exceededContextWindowSize:
            fail("ai_error", "the prompt (with history and instructions) exceeds Apple's on-device model's context window (~4096 tokens) — send less")
        case .assetsUnavailable:
            fail("model_loading", "Apple's on-device model assets are not ready yet; try again shortly")
        case .unsupportedLanguageOrLocale:
            fail("bad_request", "Apple's on-device model does not support the language of this prompt")
        case .rateLimited:
            fail("ai_error", "Apple's on-device model is rate-limiting this process; try again shortly")
        case .concurrentRequests:
            fail("ai_error", "Apple's on-device model refused a concurrent request; try again")
        default:
            fail("ai_error", error.localizedDescription)
        }
    } catch {
        fail("ai_error", error.localizedDescription)
    }
}

// MARK: - speech

func seconds(_ time: CMTime) -> Double {
    let s = CMTimeGetSeconds(time)
    return s.isFinite ? (s * 100).rounded() / 100 : 0
}

/// Decode the file's first audio track to PCM buffers in EXACTLY the analyzer's
/// format — sample rate, channel count, sample type AND layout. SpeechAnalyzer
/// traps (SIGTRAP inside `preRunRecognition`) on a buffer whose format merely
/// resembles the one it asked for, and a Float32 copy into the Int16 buffer it
/// wants on Apple Silicon is silence with the right length.
///
/// `AVAssetReader`, not `AVAudioFile`: the latter opens audio containers only,
/// and every other transcribe engine here accepts a `.mp4` or `.mov` — a page
/// must not have to know which engine ran (AI-10c).
func pcmStream(url: URL, format: AVAudioFormat) async throws -> (AsyncThrowingStream<AnalyzerInput, Error>, AVAsset) {
    let asset = AVURLAsset(url: url)
    let reader = try AVAssetReader(asset: asset)
    guard let track = try await asset.loadTracks(withMediaType: .audio).first else {
        throw NSError(domain: "fused.apple", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "the file has no audio track"])
    }
    let planar = !format.isInterleaved
    let description = format.streamDescription.pointee
    let sampleBytes = Int(description.mBitsPerChannel / 8)
    let isFloat = (description.mFormatFlags & kAudioFormatFlagIsFloat) != 0
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: format.sampleRate,
        AVNumberOfChannelsKey: format.channelCount,
        AVLinearPCMBitDepthKey: sampleBytes * 8,
        AVLinearPCMIsFloatKey: isFloat,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: planar,
    ])
    output.alwaysCopiesSampleData = false
    reader.add(output)
    let channels = Int(format.channelCount)
    let stream = AsyncThrowingStream<AnalyzerInput, Error> { continuation in
        // Every call on the reader — startReading AND the copyNextSampleBuffer
        // loop — happens on this one task: AVAssetReader is not thread-safe,
        // and reads from a thread other than the one that started it can
        // return nothing or trap. A failed start surfaces through the stream
        // as the same error a mid-file decode failure would.
        Task.detached {
            guard reader.startReading() else {
                continuation.finish(throwing: reader.error ?? NSError(
                    domain: "fused.apple", code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "the audio could not be decoded"]))
                return
            }
            while let sample = output.copyNextSampleBuffer() {
                guard let block = CMSampleBufferGetDataBuffer(sample) else { continue }
                let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sample))
                guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { continue }
                buffer.frameLength = frames
                var length = 0
                var pointer: UnsafeMutablePointer<Int8>?
                CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil,
                                            totalLengthOut: &length, dataPointerOut: &pointer)
                if let pointer {
                    // Raw bytes into the buffer's own AudioBufferList, whatever
                    // the sample type. A planar block is channel 0's frames,
                    // then channel 1's…
                    let list = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
                    let perChannel = Int(frames) * sampleBytes
                    if planar {
                        for c in 0..<min(channels, list.count) where (c + 1) * perChannel <= length {
                            if let dest = list[c].mData { memcpy(dest, pointer + c * perChannel, perChannel) }
                        }
                    } else if let dest = list[0].mData {
                        memcpy(dest, pointer, min(length, perChannel * channels))
                    }
                }
                continuation.yield(AnalyzerInput(buffer: buffer,
                                                 bufferStartTime: CMSampleBufferGetPresentationTimeStamp(sample)))
            }
            if let error = reader.error { continuation.finish(throwing: error) } else { continuation.finish() }
        }
    }
    return (stream, asset)
}

func transcribe(_ request: [String: Any]) async {
    guard let path = request["path"] as? String, FileManager.default.fileExists(atPath: path) else {
        fail("bad_request", "no such file: \(request["path"] ?? "")")
    }
    let wantsWords = request["words"] as? Bool ?? false
    let asked = (request["locale"] as? String).map { Locale(identifier: $0) } ?? Locale.current
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: asked) else {
        let supported = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }.sorted().joined(separator: ", ")
        fail("bad_request", "Apple's speech model has no \(asked.identifier(.bcp47)) — supported: \(supported)")
    }
    // `.audioTimeRange` is asked for whether or not the caller wants words:
    // the segment's own `range` needs it, and the per-word runs only exist
    // when it is on. `words` decides what is EMITTED, not what is decoded.
    let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                        reportingOptions: [], attributeOptions: [.audioTimeRange])
    do {
        // The locale's model lives in system storage and is fetched on first
        // use; one frame tells the row it is a download, not a stall.
        if let install = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            frame("assets", ["state": "installing"])
            try await install.downloadAndInstall()
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            fail("ai_error", "no audio format is compatible with Apple's speech model")
        }
        let (input, asset) = try await pcmStream(url: URL(fileURLWithPath: path), format: format)
        let duration = seconds(try await asset.load(.duration))
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // Results are consumed on their own Task so segments stream out while
        // the file is still being fed.
        let consumer = Task {
            for try await result in transcriber.results {
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
                frame("segment", segment)
            }
        }
        if let last = try await analyzer.analyzeSequence(input) {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        try await consumer.value
        frame("done", ["ok": true, "duration": duration, "locale": locale.identifier(.bcp47)])
    } catch {
        fail("ai_error", error.localizedDescription)
    }
}

// MARK: - main

/// The one request object on stdin, or an empty dict for `probe`.
func readRequest() -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.isEmpty { return [:] }
    guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fail("bad_request", "stdin must hold one JSON object")
    }
    return object
}

setvbuf(stdout, nil, _IONBF, 0)
let op = CommandLine.arguments.dropFirst().first ?? ""
Task {
    switch op {
    case "probe": await probe()
    case "text": await generateText(readRequest())
    case "speech": await transcribe(readRequest())
    default: fail("bad_request", "usage: fused-apple-ai probe|text|speech  (request JSON on stdin)")
    }
    exit(0)
}
// Cancellation is the parent terminating this process; there is nothing to
// unwind — the OS daemon owns the model and the parent owns the files.
while true { Thread.sleep(forTimeInterval: 1) }
