// capture.swift — 捕获"系统正在播放的声音"（ScreenCaptureKit，苹果官方框架）
//
// 零安装方案：不再需要 BlackHole 虚拟声卡、不需要 ffmpeg、不需要切换输出设备。
// macOS 系统自带的 ScreenCaptureKit 可直接从系统混音器取"正在播放的声音"，
// 需要一次性授予"屏幕录制"隐私权限（系统设置→隐私与安全性→屏幕录制）。
// OBS、各类会议转录软件在 Mac 上均用此接口。
//
// 用法:
//   capture --probe                         # 检查"屏幕录制"权限是否已授予
//   capture --session <DIR> [--segment 20]  # 常驻捕获系统声音 → DIR/chunks/seg_*.wav
//   capture --stop --session <DIR>          # 优雅停止（向 record.pid 发 SIGTERM）
//
// 输出与旧方案完全兼容：16kHz 单声道 16bit PCM 的 wav 分块，transcribe.py 直接可用。
//
// 注意: 需求 macOS 14+（capturesAudio 特性）。Apple Silicon 与 Intel 均可。
// 分块策略：无论有声无声都按 segment 持续落盘（静音期落全零块），先写 .part、
// 写完 rename 成 .wav（防转写读到半截）。停止时不足一段的尾块也会落盘，避免丢内容。
// 「连续静音超时」等结束判定由上层 listen.py 读分块音频能量完成，勿在此做 VAD 门控，
// 否则 mtime/落盘语义会与 listen 的能量判定产生歧义（历史教训，见 git log）。

import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreGraphics

let SAMPLE_RATE = 16000
let PROG = "capture"

// MARK: - 参数解析
struct Opts {
    var probe = false
    var stop = false
    var session: String?
    var segment = 20
}

func parseArgs() -> Opts {
    var o = Opts()
    let a = CommandLine.arguments
    var i = 1
    while i < a.count {
        switch a[i] {
        case "--probe": o.probe = true
        case "--stop": o.stop = true
        case "--session":
            i += 1
            if i < a.count { o.session = a[i] }
        case "--segment":
            i += 1
            if i < a.count, let v = Int(a[i]) { o.segment = v }
        default: break
        }
        i += 1
    }
    return o
}

func log(_ msg: String) {
    FileHandle.standardError.write(Data(("[\(PROG)] " + msg + "\n").utf8))
}

// MARK: - WAV 分块写入器
final class ChunkWriter {
    private let dir: URL
    private let segSamples: Int
    private var fh: FileHandle?
    private var currentURL: URL?
    private var samples: Int = 0
    private let lock = NSLock()

    private static let tsFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd_HHmmss_SSS"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    init(sessionDir: URL, segmentSeconds: Int) throws {
        dir = sessionDir.appendingPathComponent("chunks", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        segSamples = max(1, segmentSeconds * SAMPLE_RATE)
    }

    private func wavHeader(dataSize: Int) -> Data {
        var d = Data()
        func push(_ s: String) { d.append(contentsOf: Array(s.utf8)) }
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(withUnsafeBytes(of: &x) { Data($0) }) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(withUnsafeBytes(of: &x) { Data($0) }) }
        push("RIFF"); u32(UInt32(36 + dataSize)); push("WAVE")
        push("fmt "); u32(16); u16(1); u16(1)
        u32(UInt32(SAMPLE_RATE)); u32(UInt32(SAMPLE_RATE * 2)); u16(2); u16(16)
        push("data"); u32(UInt32(dataSize))
        return d
    }

    private func openNewLocked() throws {
        // 先写 .part 临时名，finalize 完再 rename 成 .wav：保证 transcribe
        // watch 进程永远只见到「已写完整」的块（否则会读到写了一半的 0 帧块，
        // 误判静音并永久跳过，导致整场直播转不出字）。
        let name = "seg_\(ChunkWriter.tsFormatter.string(from: Date())).wav.part"
        currentURL = dir.appendingPathComponent(name)
        let fm = FileManager.default
        guard fm.createFile(atPath: currentURL!.path, contents: nil) else {
            throw NSError(domain: PROG, code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "无法创建分块文件"])
        }
        let h = try FileHandle(forWritingTo: currentURL!)
        // 新打开的写句柄偏移在文件头，必须 seek 到末尾再写，否则覆盖已写内容
        try h.seekToEndOfFile()
        try h.write(contentsOf: wavHeader(dataSize: 0))
        fh = h
        samples = 0
    }

    /// 追加 s16 PCM 采样（任意声道数已由调用方下混为单声道）
    func append(_ mono: [Int16]) throws {
        lock.lock(); defer { lock.unlock() }
        guard !mono.isEmpty else { return }
        if fh == nil { try openNewLocked() }
        try fh?.write(contentsOf: mono.withUnsafeBytes { Data($0) })
        samples += mono.count
        if samples >= segSamples {
            try finalizeLocked()
        }
    }

    private func finalizeLocked() throws {
        guard let fh = fh, let url = currentURL else { return }
        let dataSize = samples * 2
        var a = UInt32(36 + dataSize).littleEndian
        try fh.seek(toOffset: 4)
        try fh.write(contentsOf: withUnsafeBytes(of: &a) { Data($0) })
        var b = UInt32(dataSize).littleEndian
        try fh.seek(toOffset: 40)
        try fh.write(contentsOf: withUnsafeBytes(of: &b) { Data($0) })
        try fh.close()
        self.fh = nil
        // 写完整后 rename 成 .wav（去掉 .part），转写侧只认 .wav
        if url.pathExtension == "part" {
            let finalURL = url.deletingPathExtension()
            try? FileManager.default.moveItem(at: url, to: finalURL)
        }
        let secs = String(format: "%.1f", Double(samples) / Double(SAMPLE_RATE))
        log("chunk 写出 \(url.deletingPathExtension().lastPathComponent)（\(secs)s 音频）")
    }

    /// 停止时封口尾块（不足一段也落盘）
    func close() throws {
        lock.lock(); defer { lock.unlock() }
        if fh != nil { try finalizeLocked() }
    }
}

// MARK: - CMSampleBuffer → 单声道 Int16
final class AudioConverter {
    /// 把 SCK 音频 sample buffer（float32/int16、交织或非交织、任意声道）转成 16kHz 单声道 Int16。
    /// SCStreamConfiguration 已请求 16k/单声道，此函数额外兼容万一系统仍给原生格式的情况。
    func toMonoS16(_ buf: CMSampleBuffer) -> [Int16] {
        guard let fmt = CMSampleBufferGetFormatDescription(buf),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else { return [] }
        let ab = asbd.pointee
        let ch = max(1, Int(ab.mChannelsPerFrame))
        let interleaved = (ab.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        let isFloat = (ab.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bytesPerSample = max(1, Int(ab.mBitsPerChannel) / 8)
        let frames = Int(CMSampleBufferGetNumSamples(buf))
        guard frames > 0, frames < 1_000_000 else { return [] }

        var needed = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            buf, bufferListSizeNeededOut: &needed, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: kCFAllocatorDefault, blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0, blockBufferOut: nil)
        guard needed > 0 else { return [] }
        let mem = UnsafeMutableRawPointer.allocate(
            byteCount: needed, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { mem.deallocate() }
        var block: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            buf, bufferListSizeNeededOut: nil,
            bufferListOut: mem.assumingMemoryBound(to: AudioBufferList.self),
            bufferListSize: needed,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0, blockBufferOut: &block)
        guard st == noErr else { return [] }
        let list = UnsafeMutableAudioBufferListPointer(
            mem.assumingMemoryBound(to: AudioBufferList.self))

        var mono = [Float](repeating: 0, count: frames)
        let chScale = 1.0 / Float(ch)

        func readFloat(_ base: UnsafePointer<UInt8>, _ off: Int) -> Float {
            guard bytesPerSample == 4 else { return 0 }
            var u: UInt32 = 0
            withUnsafeMutableBytes(of: &u) { raw in
                for k in 0..<4 { raw[k] = base[off + k] }
            }
            return Float(bitPattern: u)  // macOS 小端，直读即 LE
        }
        func readS16(_ base: UnsafePointer<UInt8>, _ off: Int) -> Float {
            guard bytesPerSample == 2 else { return 0 }
            var u: UInt16 = 0
            withUnsafeMutableBytes(of: &u) { raw in
                for k in 0..<2 { raw[k] = base[off + k] }
            }
            return Float(Int16(bitPattern: u)) / 32768.0
        }

        if interleaved {
            guard let first = list.first, let data = first.mData else { return [] }
            let base = data.assumingMemoryBound(to: UInt8.self)
            for c in 0..<ch {
                let read: (UnsafePointer<UInt8>, Int) -> Float = isFloat ? readFloat : readS16
                for f in 0..<frames {
                    let off = (f * ch + c) * bytesPerSample
                    mono[f] += read(base, off) * chScale
                }
            }
        } else {
            for (idx, abuf) in list.enumerated() where idx < ch {
                guard let data = abuf.mData else { continue }
                let base = data.assumingMemoryBound(to: UInt8.self)
                let n = min(frames, Int(abuf.mDataByteSize) / max(1, bytesPerSample))
                let read: (UnsafePointer<UInt8>, Int) -> Float = isFloat ? readFloat : readS16
                for f in 0..<n {
                    mono[f] += read(base, f * bytesPerSample) * chScale
                }
            }
        }

        var out = [Int16](repeating: 0, count: frames)
        for f in 0..<frames {
            let v = mono[f]
            if v >= 1 { out[f] = Int16.max }
            else if v <= -1 { out[f] = Int16.min }
            else { out[f] = Int16(v * 32767.0) }
        }
        return out
    }
}

// MARK: - 捕获器
final class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    private let writer: ChunkWriter
    private let converter = AudioConverter()
    private var stream: SCStream?
    private let sessionDir: URL

    init(sessionDir: URL, segment: Int) throws {
        self.sessionDir = sessionDir
        self.writer = try ChunkWriter(sessionDir: sessionDir, segmentSeconds: segment)
        super.init()
    }

    /// 权限检查：能取到共享内容即已授权
    static func checkPermission() async -> (ok: Bool, displays: Int, message: String) {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false)
            return (true, content.displays.count,
                    "权限正常，检测到 \(content.displays.count) 个显示器。")
        } catch {
            return (false, 0, "ScreenCaptureKit 无法访问系统声音/屏幕：\(error.localizedDescription)")
        }
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: PROG, code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "未检测到任何显示器。"])
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let config = SCStreamConfiguration()
        // 视频是 API 强制要求，压到最小（2×2 @1fps）后直接丢弃帧，只取声音
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 3
        // 关键：只取系统音频、不要麦克风、不录自身声音（防回声环）
        config.capturesAudio = true
        config.captureMicrophone = false
        config.excludesCurrentProcessAudio = true
        config.sampleRate = SAMPLE_RATE
        config.channelCount = 1

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        let q = DispatchQueue(label: "capture.samples")
        // 必须同时注册 .audio 与 .screen，只注册 audio 时 SCStream 行为异常
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: q)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
        try await s.startCapture()
        stream = s
    }

    func stop() async {
        if let s = stream {
            try? await s.stopCapture()
            stream = nil
        }
        // 等 in-flight buffer 落定再封口
        usleep(400_000)
        try? writer.close()
    }

    // SCStreamOutput
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio else { return }  // 视频帧直接丢弃
        let s16 = converter.toMonoS16(sampleBuffer)
        if !s16.isEmpty {
            try? writer.append(s16)
        }
    }

    // SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("流意外停止：\(error.localizedDescription)")
    }
}

// MARK: - 入口
@main
struct CaptureTool {
    static var keepAlive = DispatchSemaphore(value: 0)
    static var stopRequested = false

    static func runAsync(_ o: Opts) async {
        if o.probe {
            let r = await Capturer.checkPermission()
            if r.ok {
                print("[capture] \(r.message)")
            } else {
                log("未授予「屏幕录制」权限：\(r.message)")
                log("请在 系统设置 → 隐私与安全性 → 屏幕录制 中，")
                log("勾选承载本工具的 App（终端 / IDE），然后重启该 App 再试。")
                exit(3)
            }
            return
        }

        guard let session = o.session else {
            log("缺少 --session <目录>（或使用 --probe 检查权限）")
            exit(2)
        }
        let dir = URL(fileURLWithPath: session, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        if o.stop {
            let pidFile = dir.appendingPathComponent("record.pid")
            guard let s = try? String(contentsOf: pidFile, encoding: .utf8).trimmingCharacters(
                in: .whitespacesAndNewlines), let pid = Int32(s) else {
                log("未找到 record.pid（录音进程可能已退出）。")
                return
            }
            kill(pid, SIGTERM)
            log("已向录音进程 pid=\(pid) 发送结束信号。")
            return
        }

        // 权限预检
        let perm = await Capturer.checkPermission()
        if !perm.ok {
            log("「屏幕录制」权限未授予。请先在 系统设置 → 隐私与安全性 → 屏幕录制 中")
            log("勾选承载本工具的 App（终端 / IDE），重启后重试。")
            log("详情：\(perm.message)")
            exit(3)
        }

        // 单实例保护
        let pidFile = dir.appendingPathComponent("record.pid")
        if let old = try? String(contentsOf: pidFile, encoding: .utf8).trimmingCharacters(
            in: .whitespacesAndNewlines), let opid = Int32(old) {
            if kill(opid, 0) == 0 || errno != ESRCH {
                log("已有录音进程 pid=\(opid) 在运行（\(pidFile.path)），如需重启先 --stop。")
                exit(1)
            }
        }

        let capturer: Capturer
        do {
            capturer = try Capturer(sessionDir: dir, segment: o.segment)
            try await capturer.start()
        } catch {
            log("启动捕获失败：\(error.localizedDescription)")
            exit(1)
        }
        try? String(getpid()).write(to: pidFile, atomically: true, encoding: .utf8)
        // meta.json 写合并而非覆盖：listen.py / record.py 可能已写好全量元数据
        // （url/主播/主题/听课人/kind 等），这里只补缺省字段，避免整份覆盖丢失。
        do {
            let metaURL = dir.appendingPathComponent("meta.json")
            var meta: [String: Any] = [:]
            if let old = try? Data(contentsOf: metaURL),
               let obj = try? JSONSerialization.jsonObject(with: old) as? [String: Any] {
                meta = obj
            }
            if meta["started_at"] == nil {
                meta["started_at"] = ISO8601DateFormatter().string(from: Date())
            }
            if meta["engine"] == nil { meta["engine"] = "screencapturekit" }
            let out = try JSONSerialization.data(withJSONObject: meta,
                                                 options: [.prettyPrinted, .sortedKeys])
            try out.write(to: metaURL)
        } catch {
            log("meta.json 写入失败（不影响捕获）：\(error.localizedDescription)")
        }

        log("开始捕获系统声音（ScreenCaptureKit 系统自带，16kHz 单声道）→ \(dir.path)/chunks/")
        log("按 Ctrl-C 或 capture --stop --session \(session) 结束；本进程常驻。")

        // 信号处理：优雅结束（信号源须强引用保活）
        var signalSources: [DispatchSourceSignal] = []
        func installSignal(_ sig: Int32) {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler {
                guard !stopRequested else { return }
                stopRequested = true
                Task {
                    await capturer.stop()
                    log("捕获已停止，音频分块已全部落盘。")
                    try? FileManager.default.removeItem(at: pidFile)
                    keepAlive.signal()
                }
            }
            src.resume()
            signal(sig, SIG_IGN)
            signalSources.append(src)
        }
        installSignal(SIGTERM)
        installSignal(SIGINT)
        withExtendedLifetime(signalSources) {
            keepAlive.wait()
        }
        exit(0)
    }

    static func main() async {
        var o = parseArgs()
        if o.segment < 1 { o.segment = 20 }
        await runAsync(o)
    }
}
