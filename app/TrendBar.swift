// TrendBar — 今日の技術トレンド1件をメニューバーとグローバルホットキーで表示する。
// データ（~/daily-trend/data/days/YYYY-MM-DD.json）は Python 側が書く。このアプリは読むだけ。
// 第三者ライブラリなし。AppKit + SwiftUI + Carbon(ホットキーのみ)。

import AppKit
import Carbon
import SwiftUI

// MARK: - パス

/// 動作ログ（data/logs/trendbar.log）。ホットキーの受信・パネルの表示・閉じた理由を1行ずつ残す。
enum ActivityLog {
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f
    }()

    static func write(_ message: String) {
        let dir = Paths.logsDir
        let url = dir.appendingPathComponent("trendbar.log")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? Int, size > 1_000_000 {
            try? FileManager.default.removeItem(at: url)
        }
        let line = "\(formatter.string(from: Date())) \(message)\n"
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile()
            h.write(Data(line.utf8))
            try? h.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
    }
}

enum Paths {
    static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    /// コード（bin/trend）の場所。仕様どおり ~/daily-trend 固定。
    static var codeRoot: URL { home.appendingPathComponent("daily-trend", isDirectory: true) }
    /// データと config.toml の場所。Python 側（common.py の ROOT）と同じく DAILY_TREND_HOME を優先する。
    static let projectRoot: URL = resolveProjectRoot()
    static var defaultDataDir: URL { projectRoot.appendingPathComponent("data/days", isDirectory: true) }
    static var logsDir: URL { projectRoot.appendingPathComponent("data/logs", isDirectory: true) }
    static var trendCommand: URL { codeRoot.appendingPathComponent("bin/trend") }
    static var configFile: URL {
        if let e = ProcessInfo.processInfo.environment["DAILY_TREND_CONFIG"], !e.isEmpty {
            return URL(fileURLWithPath: (e as NSString).expandingTildeInPath)
        }
        return projectRoot.appendingPathComponent("config.toml")
    }

    /// DAILY_TREND_HOME の決め方: (1) このプロセスの環境変数 (2) zsh が読む ~/.zshenv の値 (3) ~/daily-trend。
    /// launchd から起動した TrendBar は ~/.zshenv を読まない。一方「今すぐ生成」と定時生成は zsh -c 経由で
    /// ~/.zshenv を読む。(2) で同じ値を使い、書く場所と読む場所をそろえる。
    private static func resolveProjectRoot() -> URL {
        let fromEnv = ProcessInfo.processInfo.environment["DAILY_TREND_HOME"].nonEmpty
        if let v = fromEnv ?? loginShellValue("DAILY_TREND_HOME") {
            return URL(fileURLWithPath: (v as NSString).expandingTildeInPath, isDirectory: true)
        }
        return codeRoot
    }

    /// /bin/zsh -c で環境変数の値を読む（~/.zshenv が効く）。3秒で打ち切る。
    static func loginShellValue(_ name: String) -> String? {
        let marker = "__TRENDBAR_VALUE__"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-c", "printf '\\n\(marker)%s' \"${\(name)-}\""]
        let out = Pipe()
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        let done = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in done.signal() }
        do { try p.run() } catch { return nil }
        if done.wait(timeout: .now() + 3) == .timedOut {
            p.terminate()
            return nil
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8),
              let r = text.range(of: marker, options: .backwards) else { return nil }
        let v = text[r.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }
}

// MARK: - データモデル（Codable。必須以外は欠けても落ちない）

struct Lossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

extension KeyedDecodingContainer {
    /// 無い・null・型違いのどれでも nil を返す
    func opt<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        guard let v = try? decodeIfPresent(type, forKey: key) else { return nil }
        return v
    }

    /// 配列。壊れた要素は捨てる。配列自体が無ければ空。
    func lossyArray<T: Decodable>(_ type: T.Type, _ key: Key) -> [T] {
        guard let arr = try? decodeIfPresent([Lossy<T>].self, forKey: key) else { return [] }
        return arr.compactMap { $0.value }
    }
}

extension Optional where Wrapped == String {
    var nonEmpty: String? {
        guard let s = self?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else { return nil }
        return s
    }
}

struct TrendSource: Decodable {
    let title: String?
    let url: String
    let site: String?
    let publishedAt: String?
    let lang: String?

    enum K: String, CodingKey { case title, url, site, published_at, lang }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        url = try c.decode(String.self, forKey: .url)
        title = c.opt(String.self, .title)
        site = c.opt(String.self, .site)
        publishedAt = c.opt(String.self, .published_at)
        lang = c.opt(String.self, .lang)
    }
}

struct TrendTerm: Decodable {
    let term: String?
    let explain: String?

    enum K: String, CodingKey { case term, explain }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        term = c.opt(String.self, .term)
        explain = c.opt(String.self, .explain)
    }
}

struct TrendBuzz: Decodable {
    let platform: String?
    let url: String?
    let metric: String?
    let observedAt: String?

    enum K: String, CodingKey { case platform, url, metric, observed_at }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        platform = c.opt(String.self, .platform)
        url = c.opt(String.self, .url)
        metric = c.opt(String.self, .metric)
        observedAt = c.opt(String.self, .observed_at)
    }
}

struct TrendEvidence: Decodable {
    let pointIndex: Int?
    let quote: String?
    let verified: Bool?

    enum K: String, CodingKey { case point_index, quote, verified }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        pointIndex = c.opt(Int.self, .point_index)
        quote = c.opt(String.self, .quote)
        verified = c.opt(Bool.self, .verified)
    }
}

struct Article: Decodable {
    static let supportedSchema = 1

    // 必須
    let schemaVersion: Int
    let date: String
    let headline: String
    let oneLiner: String
    let points: [String]
    let source: TrendSource
    // 任意
    let generatedAt: String?
    let model: String?
    let category: String?
    let whyNow: String?
    let terms: [TrendTerm]
    let buzz: [TrendBuzz]
    let evidence: [TrendEvidence]
    let warnings: [String]
    /// 見出し・結論・「なぜ今」の判定結果（"ok" / "unsupported" / "unjudged"）。古いデータには無い。
    let fieldChecks: [String: String]

    enum K: String, CodingKey {
        case schema_version, date, generated_at, model, category, headline, one_liner, points
        case why_now, terms, source, buzz, evidence, warnings, field_checks
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        schemaVersion = try c.decode(Int.self, forKey: .schema_version)
        date = try c.decode(String.self, forKey: .date)
        headline = try c.decode(String.self, forKey: .headline)
        oneLiner = try c.decode(String.self, forKey: .one_liner)
        points = try c.decode([String].self, forKey: .points)
        source = try c.decode(TrendSource.self, forKey: .source)
        generatedAt = c.opt(String.self, .generated_at)
        model = c.opt(String.self, .model)
        category = c.opt(String.self, .category)
        whyNow = c.opt(String.self, .why_now)
        terms = c.lossyArray(TrendTerm.self, .terms)
        buzz = c.lossyArray(TrendBuzz.self, .buzz)
        evidence = c.lossyArray(TrendEvidence.self, .evidence)
        warnings = c.lossyArray(String.self, .warnings)
        fieldChecks = c.opt([String: String].self, .field_checks) ?? [:]
    }

    /// field_checks で ok 以外の欄か（無いときは warnings の文言で判断する）
    func fieldFlagged(_ key: String) -> Bool? {
        guard let v = fieldChecks[key] else { return nil }
        return v != "ok"
    }

    /// evidence[].verified == false の要点の番号
    var unverifiedPointIndices: Set<Int> {
        Set(evidence.filter { $0.verified == false }.compactMap { $0.pointIndex })
    }

    /// 表示する警告（データの warnings ＋ アプリ側の注意）
    var displayWarnings: [String] {
        var w = warnings.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        if schemaVersion != Article.supportedSchema {
            w.append("schema_version \(schemaVersion) はこのアプリの想定（\(Article.supportedSchema)）と違います。表示が欠ける可能性があります")
        }
        return w
    }

    /// 警告を、見出し・結論の1文・各要点・その他に振り分けたもの
    var warningGroups: WarningGroups { WarningGroups(displayWarnings, pointCount: points.count) }

    /// ⚠︎ を付ける要点（evidence.verified == false か、その要点についての warnings がある）
    var flaggedPointIndices: Set<Int> {
        unverifiedPointIndices.union(warningGroups.byPoint.keys)
    }

    /// 「なぜ今」。要約側が「本文からは不明」と書いたときは情報が無いので nil。
    var informativeWhyNow: String? {
        guard let w = whyNow.nonEmpty else { return nil }
        let core = w.trimmingCharacters(in: CharacterSet(charactersIn: "。．. 　"))
        return ["本文からは不明", "不明"].contains(core) ? nil : w
    }

    /// D キーで開く話題元。URL を持つ最初の buzz（trend buzz と同じ）。
    var firstBuzzURL: URL? { buzz.lazy.compactMap { safeURL($0.url) }.first }

    var visibleTerms: [TrendTerm] { terms.filter { $0.term.nonEmpty != nil } }

    /// C キーでコピーする引用元（Markdown）
    var citationMarkdown: String {
        var lines: [String] = []
        let title = (source.title.nonEmpty ?? source.url)
            .replacingOccurrences(of: "[", with: "\\[")
            .replacingOccurrences(of: "]", with: "\\]")
        var first = "- [\(title)](\(source.url))"
        let tail = [source.site.nonEmpty, source.publishedAt.nonEmpty].compactMap { $0 }
        if !tail.isEmpty { first += " — " + tail.joined(separator: ", ") }
        lines.append(first)
        for b in buzz {
            var l = "- \(b.platform.nonEmpty ?? "話題元")"
            if let m = b.metric.nonEmpty { l += ": \(m)" }
            if let u = b.url.nonEmpty { l += " (\(u))" }
            lines.append(l)
        }
        return lines.joined(separator: "\n")
    }
}

/// http/https だけを開く（データは LLM が書くので念のため）
func safeURL(_ s: String?) -> URL? {
    guard let s = s.nonEmpty, let u = URL(string: s), let scheme = u.scheme?.lowercased(),
          scheme == "http" || scheme == "https" else { return nil }
    return u
}

/// warnings の振り分け。文言は daily_trend/cli.py の build_record が書く形式に合わせている:
///   「要点N: …」「見出しに…」「結論の1文に…」、それ以外（本文の切り詰め、要旨だけ等）
struct WarningGroups {
    var headline: [String] = []
    var oneLiner: [String] = []
    /// 要点の番号（0 始まり）→ 「要点N:」を除いた文
    var byPoint: [Int: [String]] = [:]
    var general: [String] = []

    init(_ warnings: [String], pointCount: Int) {
        for raw in warnings {
            let w = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if w.isEmpty { continue }
            if let (i, rest) = Self.pointRef(w), i < pointCount {
                byPoint[i, default: []].append(rest.isEmpty ? w : rest)
            } else if w.hasPrefix("見出し") {
                headline.append(w)
            } else if w.hasPrefix("結論の1文") || w.hasPrefix("結論の一文") || w.hasPrefix("結論の１文") {
                oneLiner.append(w)
            } else {
                general.append(w)
            }
        }
    }

    var isEmpty: Bool { headline.isEmpty && oneLiner.isEmpty && byPoint.isEmpty && general.isEmpty }
    var count: Int { headline.count + oneLiner.count + byPoint.values.map(\.count).reduce(0, +) + general.count }

    /// 「要点3: …」→ (2, "…")。全角数字・全角コロンも受ける。
    /// 「要点3の…」は文をそのまま返す（(2, "要点3の…")）。
    static func pointRef(_ s: String) -> (Int, String)? {
        guard s.hasPrefix("要点") else { return nil }
        var rest = Substring(s).dropFirst(2)
        var n = 0
        var digits = 0
        while let c = rest.first, let d = c.wholeNumberValue, digits < 3 {
            n = n * 10 + d
            digits += 1
            rest = rest.dropFirst()
        }
        guard digits > 0, n >= 1, let next = rest.first else { return nil }
        if next == ":" || next == "：" { return (n - 1, rest.dropFirst().trimmingCharacters(in: .whitespaces)) }
        if next == "の" { return (n - 1, s) }
        return nil
    }
}

// MARK: - 日付ユーティリティ

enum DateUtil {
    static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    static let weekdayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "ja_JP")
        f.timeZone = .current
        f.dateFormat = "E"
        return f
    }()

    static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "HH:mm"
        return f
    }()

    static let dayTimeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "M/d HH:mm"
        return f
    }()

    static let fullFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f
    }()

    static func day(_ s: String) -> Date? { dayFormatter.date(from: s) }

    /// ファイル名として正しい日付か（YYYY-MM-DD）
    static func isDayString(_ s: String) -> Bool {
        let chars = Array(s)
        guard chars.count == 10 else { return false }
        for (i, ch) in chars.enumerated() {
            if i == 4 || i == 7 { if ch != "-" { return false } } else if !ch.isASCII || !ch.isNumber { return false }
        }
        return day(s) != nil
    }

    static func daysAgo(_ s: String, now: Date = Date()) -> Int? {
        guard let d = day(s) else { return nil }
        let cal = Calendar.current
        return cal.dateComponents([.day], from: cal.startOfDay(for: d), to: cal.startOfDay(for: now)).day
    }

    static func dateLabel(_ s: String) -> String {
        guard let d = day(s) else { return s }
        return "\(s)（\(weekdayFormatter.string(from: d))）"
    }

    static func parseISO(_ s: String?) -> Date? {
        guard let s = s.nonEmpty else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: s)
    }

    /// 取得時刻。記事の日付と同じ日なら HH:mm、違えば M/d HH:mm
    static func observedLabel(_ s: String?, articleDate: String) -> String? {
        guard let d = parseISO(s) else { return nil }
        if dayFormatter.string(from: d) == articleDate { return timeFormatter.string(from: d) }
        return dayTimeFormatter.string(from: d)
    }
}

// MARK: - データの置き場所と読み込み

struct LoadError: Error {
    let message: String
}

struct DataStore {
    let dir: URL
    let fromEnv: Bool

    static func resolve() -> DataStore {
        if let e = ProcessInfo.processInfo.environment["DAILY_TREND_DATA"], !e.isEmpty {
            return DataStore(dir: URL(fileURLWithPath: (e as NSString).expandingTildeInPath, isDirectory: true), fromEnv: true)
        }
        return DataStore(dir: Paths.defaultDataDir, fromEnv: false)
    }

    /// 存在する日付（昇順）
    func availableDates() -> [String] {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { return [] }
        return names.compactMap { name -> String? in
            guard name.hasSuffix(".json") else { return nil }
            let stem = String(name.dropLast(5))
            return DateUtil.isDayString(stem) ? stem : nil
        }.sorted()
    }

    func fileURL(for date: String) -> URL { dir.appendingPathComponent("\(date).json") }

    func load(date: String) -> Result<Article, LoadError> {
        let url = fileURL(for: date)
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return .failure(LoadError(message: "ファイルを読めません: \(error.localizedDescription)"))
        }
        do {
            return .success(try JSONDecoder().decode(Article.self, from: data))
        } catch {
            return .failure(LoadError(message: Self.describe(error)))
        }
    }

    static func describe(_ error: Error) -> String {
        func path(_ keys: [CodingKey]) -> String {
            keys.map { k in k.intValue.map { "[\($0)]" } ?? k.stringValue }.joined(separator: ".")
        }
        guard let e = error as? DecodingError else { return error.localizedDescription }
        switch e {
        case .keyNotFound(let key, let ctx):
            return "必須フィールドがありません: \(path(ctx.codingPath + [key]))"
        case .typeMismatch(_, let ctx):
            return "型が違います: \(path(ctx.codingPath))"
        case .valueNotFound(_, let ctx):
            return "値が null です: \(path(ctx.codingPath))"
        case .dataCorrupted(let ctx):
            return "JSON として読めません: \(ctx.debugDescription)"
        @unknown default:
            return error.localizedDescription
        }
    }
}

// MARK: - 既読管理

enum SeenStore {
    static let key = "lastSeenDate"

    static var lastSeen: String? { UserDefaults.standard.string(forKey: key) }

    static func markSeen(_ date: String) {
        if let s = lastSeen, s >= date { return }
        UserDefaults.standard.set(date, forKey: key)
    }

    static func isUnread(latest: String?) -> Bool {
        guard let latest else { return false }
        guard let s = lastSeen else { return true }
        return latest > s
    }
}

// MARK: - パネル位置（ドラッグで動かした位置を覚える）

enum PanelPositionStore {
    static let key = "panelTopLeft"

    /// パネルの左上の位置（画面座標）。動かしていなければ nil
    static var topLeft: NSPoint? {
        guard let s = UserDefaults.standard.string(forKey: key) else { return nil }
        return NSPointFromString(s)
    }

    static func save(_ p: NSPoint) {
        UserDefaults.standard.set(NSStringFromPoint(p), forKey: key)
    }

    static func clear() {
        UserDefaults.standard.removeObject(forKey: key)
    }
}

// MARK: - ホットキー設定

struct HotKeySpec {
    let keyCode: UInt32
    let carbonModifiers: UInt32
    let display: String
    let raw: String

    static let keyCodes: [String: Int] = [
        "a": kVK_ANSI_A, "b": kVK_ANSI_B, "c": kVK_ANSI_C, "d": kVK_ANSI_D, "e": kVK_ANSI_E,
        "f": kVK_ANSI_F, "g": kVK_ANSI_G, "h": kVK_ANSI_H, "i": kVK_ANSI_I, "j": kVK_ANSI_J,
        "k": kVK_ANSI_K, "l": kVK_ANSI_L, "m": kVK_ANSI_M, "n": kVK_ANSI_N, "o": kVK_ANSI_O,
        "p": kVK_ANSI_P, "q": kVK_ANSI_Q, "r": kVK_ANSI_R, "s": kVK_ANSI_S, "t": kVK_ANSI_T,
        "u": kVK_ANSI_U, "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X, "y": kVK_ANSI_Y,
        "z": kVK_ANSI_Z,
        "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3, "4": kVK_ANSI_4,
        "5": kVK_ANSI_5, "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
    ]

    static let `default`: HotKeySpec = HotKeySpec.parse("ctrl+option+t")!

    /// "ctrl+option+t" 形式。ctrl/option/cmd のどれか1つは必須（shift だけだと通常の入力を奪うため）。
    static func parse(_ text: String) -> HotKeySpec? {
        let tokens = text.lowercased().split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
        guard tokens.count >= 2, !tokens.contains(where: { $0.isEmpty }) else { return nil }
        guard let key = tokens.last, let code = keyCodes[key] else { return nil }
        var ctrl = false, opt = false, cmd = false, shift = false
        for m in tokens.dropLast() {
            switch m {
            case "ctrl", "control": ctrl = true
            case "option", "alt", "opt": opt = true
            case "cmd", "command": cmd = true
            case "shift": shift = true
            default: return nil
            }
        }
        guard ctrl || opt || cmd else { return nil }
        var mods = 0
        var disp = ""
        if ctrl { mods |= controlKey; disp += "⌃" }
        if opt { mods |= optionKey; disp += "⌥" }
        if shift { mods |= shiftKey; disp += "⇧" }
        if cmd { mods |= cmdKey; disp += "⌘" }
        disp += key.uppercased()
        return HotKeySpec(keyCode: UInt32(code), carbonModifiers: UInt32(mods), display: disp, raw: text)
    }
}

struct HotKeyConfig {
    let spec: HotKeySpec
    let source: String
    let note: String?
}

enum ConfigLoader {
    static func hotKey() -> HotKeyConfig {
        let url = Paths.configFile
        let fallback = "⌃⌥T を使います"
        guard FileManager.default.fileExists(atPath: url.path) else {
            return HotKeyConfig(spec: .default, source: "既定値（\(url.path) が無い）", note: nil)
        }
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            return HotKeyConfig(spec: .default, source: "既定値",
                                note: "config.toml を UTF-8 として読めません。\(fallback)")
        }
        let doc: MiniTOML.Document
        switch MiniTOML.parse(text) {
        case .failure(let e):
            return HotKeyConfig(spec: .default, source: "既定値",
                                note: "config.toml の \(e.line) 行目を読めません（\(e.message)）。\(fallback)")
        case .success(let d):
            doc = d
        }
        switch doc.values[["app", "hotkey"]] {
        case .string(let raw)?:
            guard let spec = HotKeySpec.parse(raw) else {
                return HotKeyConfig(spec: .default, source: "既定値",
                                    note: "config.toml の hotkey = \"\(raw)\" を解釈できません。\(fallback)")
            }
            return HotKeyConfig(spec: spec, source: url.path, note: nil)
        case .bare(let raw)?:
            return HotKeyConfig(spec: .default, source: "既定値",
                                note: "config.toml の hotkey = \(raw) は引用符で囲んでください（例: \"ctrl+option+t\"）。\(fallback)")
        case .other?:
            return HotKeyConfig(spec: .default, source: "既定値",
                                note: "config.toml の hotkey が文字列ではありません。\(fallback)")
        case nil:
            if doc.keyNames.contains("hotkey") {
                // [app] 以外の場所に hotkey がある（書く場所の間違い）
                return HotKeyConfig(spec: .default, source: "既定値",
                                    note: "config.toml の hotkey を [app] の中で見つけられませんでした。\(fallback)")
            }
            return HotKeyConfig(spec: .default, source: "既定値（\(url.path) に [app] hotkey が無い）", note: nil)
        }
    }
}

/// TOML の最小限の読み取り（ホットキー設定のためだけ）。
/// 扱うもの: コメント、[表] / [[配列表]] 見出し、ドット付きキー、インラインテーブル、配列（中身は読み飛ばす）、
/// 4種類の文字列（"…" '…' """…""" '''…'''）。数値・真偽値・日時は bare として文字のまま持つ。
struct MiniTOML {
    enum Value: Equatable {
        case string(String)
        case bare(String)
        case other
    }

    struct Document {
        /// 完全なキーのパス（例: ["app", "hotkey"]）→ 値。[[配列表]] と配列の中は含めない。
        var values: [[String]: Value] = [:]
        /// どこかで使われたキー名（パスの最後の要素）。配列表や配列の中も含む。
        var keyNames: Set<String> = []
    }

    struct ParseError: Error {
        let line: Int
        let message: String
    }

    static func parse(_ text: String) -> Result<Document, ParseError> {
        var p = MiniTOML(text.replacingOccurrences(of: "\u{FEFF}", with: ""))
        do {
            try p.run()
            return .success(p.doc)
        } catch let e as ParseError {
            return .failure(e)
        } catch {
            return .failure(ParseError(line: p.lineNumber, message: "\(error)"))
        }
    }

    private let c: [Character]
    private var i = 0
    private var doc = Document()
    /// 今の表のパス。nil は [[配列表]] の中（値は記録しない）。
    private var table: [String]? = []

    private init(_ text: String) { c = Array(text) }

    var lineNumber: Int { c[..<min(i, c.count)].filter(\.isNewline).count + 1 }

    private func fail(_ message: String) -> ParseError { ParseError(line: lineNumber, message: message) }

    private func peek(_ k: Int = 0) -> Character? { i + k < c.count ? c[i + k] : nil }

    private func starts(with s: String) -> Bool {
        var k = 0
        for ch in s {
            guard peek(k) == ch else { return false }
            k += 1
        }
        return true
    }

    private mutating func skipSpaces() {
        while let ch = peek(), ch == " " || ch == "\t" { i += 1 }
    }

    private mutating func skipComment() {
        while let ch = peek(), !ch.isNewline { i += 1 }
    }

    /// 空白・改行・コメントを飛ばす
    private mutating func skipBlank() {
        while let ch = peek() {
            if ch == " " || ch == "\t" || ch.isNewline { i += 1 } else if ch == "#" { skipComment() } else { return }
        }
    }

    private mutating func run() throws {
        while true {
            skipBlank()
            guard let ch = peek() else { return }
            if ch == "[" { try parseHeader() } else { try parseKeyValue(prefix: table) }
            try endOfLine()
        }
    }

    private mutating func endOfLine() throws {
        skipSpaces()
        if peek() == "#" { skipComment() }
        guard let ch = peek() else { return }
        guard ch.isNewline else { throw fail("行末に余分な文字があります") }
        i += 1
    }

    private mutating func parseHeader() throws {
        i += 1
        let isArray = peek() == "["
        if isArray { i += 1 }
        skipSpaces()
        let path = try parseKeyPath()
        skipSpaces()
        guard peek() == "]" else { throw fail("表の見出しが ] で閉じていません") }
        i += 1
        if isArray {
            guard peek() == "]" else { throw fail("配列表の見出しが ]] で閉じていません") }
            i += 1
        }
        table = isArray ? nil : path
    }

    private mutating func parseKeyPath() throws -> [String] {
        var parts: [String] = []
        while true {
            skipSpaces()
            parts.append(try parseKey())
            skipSpaces()
            guard peek() == "." else { return parts }
            i += 1
        }
    }

    private mutating func parseKey() throws -> String {
        guard let ch = peek() else { throw fail("キーがありません") }
        if ch == "\"" { return try basicString() }
        if ch == "'" { return try literalString() }
        var s = ""
        while let ch = peek(), ch.isASCII, ch.isLetter || ch.isNumber || ch == "_" || ch == "-" {
            s.append(ch)
            i += 1
        }
        guard !s.isEmpty else { throw fail("キーがありません") }
        return s
    }

    /// key = value。prefix が nil なら値は記録しない（配列表や配列の中）。
    private mutating func parseKeyValue(prefix: [String]?) throws {
        let key = try parseKeyPath()
        if let last = key.last { doc.keyNames.insert(last) }
        skipSpaces()
        guard peek() == "=" else { throw fail("= がありません") }
        i += 1
        skipSpaces()
        let path = prefix.map { $0 + key }
        let v = try parseValue(path: path)
        if let path { doc.values[path] = v }
    }

    private mutating func parseValue(path: [String]?) throws -> Value {
        guard let ch = peek(), !ch.isNewline else { throw fail("値がありません") }
        if starts(with: "\"\"\"") { return .string(try multilineString(quote: "\"", escapes: true)) }
        if starts(with: "'''") { return .string(try multilineString(quote: "'", escapes: false)) }
        if ch == "\"" { return .string(try basicString()) }
        if ch == "'" { return .string(try literalString()) }
        if ch == "{" {
            i += 1
            skipBlank()
            if peek() == "}" { i += 1; return .other }
            while true {
                skipBlank()
                try parseKeyValue(prefix: path)
                skipBlank()
                if peek() == "," { i += 1; continue }
                if peek() == "}" { i += 1; return .other }
                throw fail("インラインテーブルが } で閉じていません")
            }
        }
        if ch == "[" {
            i += 1
            while true {
                skipBlank()
                if peek() == "]" { i += 1; return .other }
                _ = try parseValue(path: nil)
                skipBlank()
                if peek() == "," { i += 1; continue }
                if peek() == "]" { i += 1; return .other }
                throw fail("配列が ] で閉じていません")
            }
        }
        var s = ""
        while let ch = peek(), !ch.isNewline, ch != "#", ch != ",", ch != "]", ch != "}" {
            s.append(ch)
            i += 1
        }
        let t = s.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { throw fail("値がありません") }
        return .bare(t)
    }

    private mutating func basicString() throws -> String {
        i += 1
        var s = ""
        while true {
            guard let ch = peek(), !ch.isNewline else { throw fail("文字列が \" で閉じていません") }
            i += 1
            if ch == "\"" { return s }
            if ch == "\\" { s += try escape() } else { s.append(ch) }
        }
    }

    private mutating func literalString() throws -> String {
        i += 1
        var s = ""
        while true {
            guard let ch = peek(), !ch.isNewline else { throw fail("文字列が ' で閉じていません") }
            i += 1
            if ch == "'" { return s }
            s.append(ch)
        }
    }

    /// """…""" と '''…'''。閉じの直前に引用符を2つまで含められる（TOML の規則）。
    private mutating func multilineString(quote: Character, escapes: Bool) throws -> String {
        let triple = String(repeating: quote, count: 3)
        i += 3
        if let ch = peek(), ch.isNewline { i += 1 }
        var s = ""
        while true {
            guard let ch = peek() else { throw fail("複数行の文字列が \(triple) で閉じていません") }
            if starts(with: triple) {
                var n = 0
                while peek(n) == quote { n += 1 }
                guard n <= 5 else { throw fail("引用符が多すぎます") }
                s += String(repeating: quote, count: n - 3)
                i += n
                return s
            }
            i += 1
            if escapes && ch == "\\" {
                // 行末の \ は、改行と次の行頭の空白をまとめて消す
                var k = 0
                while let w = peek(k), w == " " || w == "\t" { k += 1 }
                if let w = peek(k), w.isNewline {
                    i += k
                    while let w = peek(), w == " " || w == "\t" || w.isNewline { i += 1 }
                    continue
                }
                s += try escape()
            } else {
                s.append(ch)
            }
        }
    }

    /// \ の直後から読む
    private mutating func escape() throws -> String {
        guard let e = peek() else { throw fail("\\ の後に文字がありません") }
        i += 1
        switch e {
        case "n": return "\n"
        case "t": return "\t"
        case "r": return "\r"
        case "b": return "\u{08}"
        case "f": return "\u{0C}"
        case "e": return "\u{1B}"
        case "\"": return "\""
        case "\\": return "\\"
        case "u", "U":
            let n = e == "u" ? 4 : 8
            var hex = ""
            for _ in 0..<n {
                guard let h = peek(), h.isHexDigit else { throw fail("\\\(e) の後に16進数が \(n) 桁ありません") }
                hex.append(h)
                i += 1
            }
            guard let v = UInt32(hex, radix: 16), let u = Unicode.Scalar(v) else { throw fail("不正な Unicode 値です") }
            return String(Character(u))
        default:
            throw fail("不明なエスケープ \\\(e) です")
        }
    }
}

// MARK: - グローバルホットキー（Carbon。アクセシビリティ権限は不要）

private func fourCC(_ s: String) -> OSType {
    s.utf8.prefix(4).reduce(0) { ($0 << 8) | OSType($1) }
}

final class HotKeyManager {
    static let signature: OSType = fourCC("TrBr")
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    var onPress: (() -> Void)?

    func installHandler() -> OSStatus {
        guard handlerRef == nil else { return noErr }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        return InstallEventHandler(GetApplicationEventTarget(), hotKeyEventHandler, 1, &spec, me, &handlerRef)
    }

    /// exclusive で登録する。
    /// - 他のアプリが同じキーを exclusive で持っていれば eventHotKeyExistsErr が返る（常駐中の TrendBar 同士も検出できる）。
    /// - 他のアプリが通常（非 exclusive）で持っている場合は検出できない。そのときは TrendBar だけが反応し、相手には届かない。
    func register(_ spec: HotKeySpec) -> OSStatus {
        unregister()
        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: HotKeyManager.signature, id: 1)
        let status = RegisterEventHotKey(spec.keyCode, spec.carbonModifiers, id, GetApplicationEventTarget(),
                                         OptionBits(kEventHotKeyExclusive), &ref)
        if status == noErr { hotKeyRef = ref }
        return status
    }

    func unregister() {
        if let r = hotKeyRef {
            UnregisterEventHotKey(r)
            hotKeyRef = nil
        }
    }

    func removeHandler() {
        if let h = handlerRef {
            RemoveEventHandler(h)
            handlerRef = nil
        }
    }

    fileprivate func fire() { onPress?() }

    static func describe(_ status: OSStatus) -> String {
        switch Int(status) {
        case Int(noErr): return "成功"
        case eventHotKeyExistsErr: return "失敗: eventHotKeyExistsErr（他のアプリか常駐中の TrendBar が同じキーを使っている）"
        case eventHotKeyInvalidErr: return "失敗: eventHotKeyInvalidErr"
        case paramErr: return "失敗: paramErr"
        default: return "失敗: OSStatus \(status)"
        }
    }
}

private func hotKeyEventHandler(_ next: EventHandlerCallRef?, _ event: EventRef?, _ userData: UnsafeMutableRawPointer?) -> OSStatus {
    guard let event, let userData else { return OSStatus(eventNotHandledErr) }
    var hk = EventHotKeyID()
    let st = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                               nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
    guard st == noErr, hk.signature == HotKeyManager.signature else { return OSStatus(eventNotHandledErr) }
    let manager = Unmanaged<HotKeyManager>.fromOpaque(userData).takeUnretainedValue()
    DispatchQueue.main.async { manager.fire() }
    return noErr
}

// MARK: - 表示状態

struct DisplayState {
    enum Content {
        case empty(dataDir: String)
        case failed(file: String, message: String)
        case article(Article)
    }

    var content: Content
    var date: String?
    var isLatest: Bool = true
    /// 表示中の日付の位置（0 始まり）と、保存されている日数
    var index: Int?
    var count: Int = 0
    var toast: String?

    var article: Article? {
        if case .article(let a) = content { return a }
        return nil
    }

    /// 過去の日を見ているときだけ出す位置の説明（最新の日では出さない）
    var positionText: String? {
        guard !isLatest, let index, count > 0 else { return nil }
        return "保存 \(count)日分の \(index + 1)日目"
    }
}

@MainActor
final class PanelModel: ObservableObject {
    @Published private(set) var state: DisplayState
    /// 内容がパネルの高さに収まらず、スクロールが要るか（PanelController が設定する）
    @Published var overflows = false
    /// スクロール領域の下にまだ見えていない内容があるか（PanelController が設定する）
    @Published var moreBelow = false
    let store: DataStore
    private(set) var dates: [String] = []
    private(set) var index: Int?
    private var toastToken = 0

    init(store: DataStore) {
        self.store = store
        state = DisplayState(content: .empty(dataDir: store.dir.path))
    }

    var currentDate: String? { index.map { dates[$0] } }
    var isShowingLatest: Bool { index != nil && index == dates.count - 1 }

    /// ディスクから読み直して最新日を表示
    func reloadLatest() {
        dates = store.availableDates()
        index = dates.isEmpty ? nil : dates.count - 1
        rebuild()
    }

    @discardableResult
    func select(date: String) -> Bool {
        dates = store.availableDates()
        guard let i = dates.firstIndex(of: date) else { return false }
        index = i
        rebuild()
        return true
    }

    @discardableResult
    func move(_ delta: Int) -> Bool {
        guard let i = index else { return false }
        let j = i + delta
        guard dates.indices.contains(j) else { return false }
        index = j
        rebuild()
        return true
    }

    func flash(_ message: String) {
        toastToken += 1
        let token = toastToken
        state.toast = message
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
            guard let self, self.toastToken == token else { return }
            self.state.toast = nil
        }
    }

    private func rebuild() {
        guard let i = index else {
            state = DisplayState(content: .empty(dataDir: store.dir.path))
            return
        }
        let d = dates[i]
        let content: DisplayState.Content
        switch store.load(date: d) {
        case .success(let a): content = .article(a)
        case .failure(let e): content = .failed(file: store.fileURL(for: d).path, message: e.message)
        }
        state = DisplayState(content: content, date: d, isLatest: i == dates.count - 1,
                             index: i, count: dates.count, toast: nil)
    }
}

// MARK: - SwiftUI ビュー

extension View {
    /// 日本語の長文を横に切らず縦に折り返す
    func wrapText() -> some View {
        fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

enum Style {
    static let panelWidth: CGFloat = 560
    static let hPad: CGFloat = 22
    static let link = Color(nsColor: .linkColor)
    /// 注意の色。ライトは白地で約 6:1 の濃いオレンジ（systemOrange は 2.3:1 で読みにくい）、ダークは systemOrange。
    static let warn = Color(nsColor: dynamic(light: NSColor(srgbRed: 0.63, green: 0.29, blue: 0.0, alpha: 1),
                                             dark: .systemOrange))
    /// 成功の色（トースト）。ライトは白地で約 5.6:1 の濃い緑。
    static let ok = Color(nsColor: dynamic(light: NSColor(srgbRed: 0.0, green: 0.45, blue: 0.15, alpha: 1),
                                           dark: .systemGreen))
    /// 補助テキストの色。.secondary（白地で約 3.9:1）より濃くする（白地で約 6.7:1、黒地で約 7.6:1）。
    static let muted = Color.primary.opacity(0.75)

    static func dynamic(light: NSColor, dark: NSColor) -> NSColor {
        NSColor(name: nil) { ap in
            ap.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        }
    }
}

struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold))
            .foregroundColor(Style.muted)
    }
}

struct LinkLabel: View {
    let text: String
    let url: URL?
    var size: CGFloat = 12
    var weight: Font.Weight = .regular
    @State private var hovering = false

    var body: some View {
        if let url {
            Button {
                NSWorkspace.shared.open(url)
            } label: {
                Text(text)
                    .font(.system(size: size, weight: weight))
                    .foregroundColor(Style.link)
                    .underline(hovering)
                    .multilineTextAlignment(.leading)
                    .wrapText()
            }
            .buttonStyle(.plain)
            .help(url.absoluteString)
            .onHover { inside in
                hovering = inside
                (inside ? NSCursor.pointingHand : NSCursor.arrow).set()
            }
        } else {
            Text(text)
                .font(.system(size: size, weight: weight))
                .wrapText()
        }
    }
}

/// 小さい注意書き1行（⚠︎ ＋ 文）
struct WarningLine: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text("⚠︎")
                .font(.system(size: 11))
            Text(text)
                .font(.system(size: 11.5))
                .wrapText()
        }
        .foregroundColor(Style.warn)
    }
}

struct MetaRow: View {
    let date: String
    let category: String?
    let isLatest: Bool
    let positionText: String?

    var body: some View {
        HStack(spacing: 6) {
            Text(DateUtil.dateLabel(date))
            if let c = category.nonEmpty {
                Text(c)
                    .font(.system(size: 10.5, weight: .medium))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1.5)
                    .background(Capsule().fill(Color.primary.opacity(0.08)))
            }
            if let ago = agoText {
                Text(ago)
                    .foregroundColor(isLatest ? Style.muted : Style.warn)
            }
            Spacer(minLength: 4)
            if let positionText {
                Text(positionText).monospacedDigit()
            }
        }
        .font(.system(size: 11))
        .foregroundColor(Style.muted)
    }

    private var agoText: String? {
        guard let n = DateUtil.daysAgo(date), n > 0 else { return nil }
        return isLatest ? "\(n)日前" : "\(n)日前（最新ではありません）"
    }
}

struct PointsView: View {
    let points: [String]
    let flagged: Set<Int>
    /// 要点ごとの注意（「要点N:」を除いた文）
    let notes: [Int: [String]]

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(points.enumerated()), id: \.offset) { i, p in
                let bad = flagged.contains(i)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(i + 1).")
                        .font(.system(size: 13, weight: .semibold).monospacedDigit())
                        .foregroundColor(Style.muted)
                        .frame(minWidth: 17, alignment: .trailing)
                    VStack(alignment: .leading, spacing: 2) {
                        pointText(p, flagged: bad)
                            .font(.system(size: 13))
                            .lineSpacing(2)
                            .wrapText()
                        ForEach(Array(noteLines(i, flagged: bad).enumerated()), id: \.offset) { _, n in
                            WarningLine(text: n)
                        }
                    }
                }
                .help(bad ? "この要点の根拠となる引用を元記事の本文で確認できませんでした" : "")
            }
        }
    }

    private func noteLines(_ i: Int, flagged: Bool) -> [String] {
        if let n = notes[i], !n.isEmpty { return n }
        return flagged ? ["根拠の引用を元記事の本文で確認できませんでした"] : []
    }

    private func pointText(_ p: String, flagged: Bool) -> Text {
        guard flagged else { return Text(p) }
        return Text(p) + Text("  ⚠︎").font(.system(size: 11)).foregroundColor(Style.warn)
    }
}

struct CitationView: View {
    let article: Article

    var body: some View {
        let src = article.source
        VStack(alignment: .leading, spacing: 4) {
            SectionLabel("引用元")
            LinkLabel(text: src.title.nonEmpty ?? src.url, url: safeURL(src.url), size: 13, weight: .medium)
            let meta = [src.site.nonEmpty, src.publishedAt.nonEmpty.map { "公開 \($0)" }].compactMap { $0 }
            if !meta.isEmpty {
                Text(meta.joined(separator: " ・ "))
                    .font(.system(size: 11.5))
                    .foregroundColor(Style.muted)
                    .wrapText()
            }
            if src.title.nonEmpty != nil {
                Text(src.url)
                    .font(.system(size: 11))
                    .foregroundColor(Style.muted)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if !article.buzz.isEmpty {
                SectionLabel("話題元")
                    .padding(.top, 6)
                ForEach(Array(article.buzz.enumerated()), id: \.offset) { _, b in
                    LinkLabel(text: buzzLine(b), url: safeURL(b.url), size: 12)
                }
            }
        }
    }

    private func buzzLine(_ b: TrendBuzz) -> String {
        var s = b.platform.nonEmpty ?? "話題元"
        if let m = b.metric.nonEmpty { s += " — \(m)" }
        if let t = DateUtil.observedLabel(b.observedAt, articleDate: article.date) { s += "（取得: \(t)）" }
        return s
    }
}

struct ArticleView: View {
    let article: Article
    let state: DisplayState

    var body: some View {
        let groups = article.warningGroups
        VStack(alignment: .leading, spacing: 12) {
            MetaRow(date: state.date ?? article.date, category: article.category,
                    isLatest: state.isLatest, positionText: state.positionText)
            VStack(alignment: .leading, spacing: 6) {
                flaggedText(article.headline, flagged: article.fieldFlagged("headline") ?? !groups.headline.isEmpty, markSize: 13)
                    .font(.system(size: 17, weight: .bold))
                    .lineSpacing(2)
                    .wrapText()
                    .help(groups.headline.first ?? "")
                flaggedText(article.oneLiner, flagged: article.fieldFlagged("one_liner") ?? !groups.oneLiner.isEmpty, markSize: 11)
                    .font(.system(size: 13.5))
                    .lineSpacing(2)
                    .wrapText()
                    .help(groups.oneLiner.first ?? "")
                let headWarnings = groups.headline + groups.oneLiner
                if !headWarnings.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(headWarnings.enumerated()), id: \.offset) { _, w in
                            WarningLine(text: w)
                        }
                    }
                }
            }
            if !article.points.isEmpty {
                PointsView(points: article.points, flagged: article.flaggedPointIndices, notes: groups.byPoint)
            }
            if !groups.general.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(groups.general.enumerated()), id: \.offset) { _, w in
                        WarningLine(text: w)
                    }
                }
            }
            if let why = article.informativeWhyNow {
                VStack(alignment: .leading, spacing: 3) {
                    SectionLabel("なぜ今")
                    flaggedText(why, flagged: article.fieldFlagged("why_now") ?? false, markSize: 10)
                        .font(.system(size: 12.5))
                        .lineSpacing(1.5)
                        .wrapText()
                }
            }
            if !article.visibleTerms.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    SectionLabel("用語（本文ではなく一般知識による説明）")
                    ForEach(Array(article.visibleTerms.enumerated()), id: \.offset) { _, t in
                        termText(t)
                            .font(.system(size: 12))
                            .foregroundColor(Style.muted)
                            .lineSpacing(1.5)
                            .wrapText()
                    }
                }
            }
            Divider()
            CitationView(article: article)
        }
    }

    private func flaggedText(_ s: String, flagged: Bool, markSize: CGFloat) -> Text {
        guard flagged else { return Text(s) }
        return Text(s) + Text("  ⚠︎").font(.system(size: markSize)).foregroundColor(Style.warn)
    }

    private func termText(_ t: TrendTerm) -> Text {
        let head = Text(t.term ?? "").fontWeight(.semibold).foregroundColor(.primary)
        guard let e = t.explain.nonEmpty else { return head }
        return head + Text("：" + e)
    }
}

struct EmptyStateView: View {
    let dataDir: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("まだ生成されていません。")
                .font(.system(size: 15, weight: .bold))
                .wrapText()
            Text("メニューの『今すぐ生成』か、ターミナルで trend run")
                .font(.system(size: 13))
                .wrapText()
            Text("データフォルダ: " + dataDir)
                .font(.system(size: 11))
                .foregroundColor(Style.muted)
                .wrapText()
        }
    }
}

struct FailedStateView: View {
    let date: String?
    let file: String
    let message: String
    let isLatest: Bool
    let positionText: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let date {
                MetaRow(date: date, category: nil, isLatest: isLatest, positionText: positionText)
            }
            Text("この日のデータを読み込めませんでした")
                .font(.system(size: 15, weight: .bold))
                .wrapText()
            Text(message)
                .font(.system(size: 12))
                .foregroundColor(Style.warn)
                .wrapText()
            Text(file)
                .font(.system(size: 11))
                .foregroundColor(Style.muted)
                .wrapText()
            Text("←→ で他の日付に移動できます")
                .font(.system(size: 11))
                .foregroundColor(Style.muted)
        }
    }
}

/// キー名を枠付きで表示する
struct KeyCap: View {
    let key: String

    var body: some View {
        Text(key)
            .font(.system(size: 10.5, weight: .semibold))
            .foregroundColor(.primary)
            .padding(.horizontal, 4.5)
            .padding(.vertical, 1)
            .frame(minWidth: 17)
            .background(RoundedRectangle(cornerRadius: 4, style: .continuous).fill(Color.primary.opacity(0.07)))
            .overlay(RoundedRectangle(cornerRadius: 4, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.25), lineWidth: 0.5))
    }
}

struct KeyHints: View {
    struct Hint: Hashable {
        let key: String
        let label: String
    }

    var scrollable = false

    private var hints: [Hint] {
        var h = [Hint(key: "Esc", label: "閉じる"), Hint(key: "O", label: "元記事"), Hint(key: "D", label: "話題元"),
                 Hint(key: "←→", label: "日付"), Hint(key: "C", label: "引用コピー")]
        if scrollable { h.append(Hint(key: "↑↓", label: "スクロール")) }
        return h
    }

    var body: some View {
        HStack(spacing: 11) {
            ForEach(hints, id: \.self) { h in
                HStack(spacing: 4) {
                    KeyCap(key: h.key)
                    Text(h.label)
                        .font(.system(size: 11))
                        .foregroundColor(Style.muted)
                }
            }
            Spacer(minLength: 0)
        }
    }
}

struct FooterView: View {
    let state: DisplayState
    var scrollable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let g = generatedLine {
                Text(g)
                    .font(.system(size: 11))
                    .foregroundColor(Style.muted)
                    .wrapText()
            }
            // トーストはキー操作ヒントと同じ場所に重ねる（高さが変わらないように）
            ZStack(alignment: .leading) {
                KeyHints(scrollable: scrollable)
                    .opacity(state.toast == nil ? 1 : 0)
                if let toast = state.toast {
                    Text(toast)
                        .font(.system(size: 11.5, weight: .semibold))
                        .foregroundColor(Style.ok)
                }
            }
        }
    }

    private var generatedLine: String? {
        guard let a = state.article else { return nil }
        var parts: [String] = []
        if let d = DateUtil.parseISO(a.generatedAt) {
            parts.append("生成 " + DateUtil.fullFormatter.string(from: d))
        }
        if let m = a.model.nonEmpty { parts.append(m) }
        parts.append("AI による要約。数値や主張は元記事で確認")
        return parts.joined(separator: " ・ ")
    }
}

/// スクロールする部分（記事・空・読込失敗）
struct TrendBodyView: View {
    let state: DisplayState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch state.content {
            case .empty(let dir):
                EmptyStateView(dataDir: dir)
            case .failed(let file, let message):
                FailedStateView(date: state.date, file: file, message: message,
                                isLatest: state.isLatest, positionText: state.positionText)
            case .article(let a):
                ArticleView(article: a, state: state)
            }
        }
        .padding(.horizontal, Style.hPad)
        .padding(.top, 18)
        .padding(.bottom, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// パネル下端に固定する部分（区切り線・生成情報・キー操作ヒント）
struct PanelFooter: View {
    let state: DisplayState
    var scrollable = false

    var body: some View {
        VStack(spacing: 0) {
            Divider()
                .padding(.horizontal, Style.hPad)
            FooterView(state: state, scrollable: scrollable)
                .padding(.horizontal, Style.hPad)
                .padding(.top, 8)
                .padding(.bottom, 12)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// パネルの中身をスクロールなしで全部並べたもの。PNG 描画と高さ計測に使う。
struct TrendContentView: View {
    let state: DisplayState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            TrendBodyView(state: state)
            PanelFooter(state: state)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// スクロール領域の下にまだ内容があることを示す印
struct MoreBelowBadge: View {
    var body: some View {
        Text("↓ 続きあり")
            .font(.system(size: 11, weight: .medium))
            .foregroundColor(.primary)
            .padding(.horizontal, 10)
            .padding(.vertical, 3)
            .background(Capsule().fill(.regularMaterial))
            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.18), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.15), radius: 3, y: 1)
            .padding(.bottom, 8)
            .padding(.trailing, 18)
            .allowsHitTesting(false)
    }
}

/// パネルに載せるルート。本文だけスクロールし、フッターは下端に固定する。
struct PanelRootView: View {
    @ObservedObject var model: PanelModel

    var body: some View {
        VStack(spacing: 0) {
            ScrollView(.vertical, showsIndicators: true) {
                TrendBodyView(state: model.state)
            }
            .overlay(alignment: .bottomTrailing) {
                if model.overflows && model.moreBelow { MoreBelowBadge() }
            }
            .id(model.state.date ?? "-")
            PanelFooter(state: model.state, scrollable: model.overflows)
        }
    }
}

// MARK: - パネル（NSPanel）

final class TrendPanel: NSPanel {
    var keyHandler: ((NSEvent) -> Bool)?

    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: Style.panelWidth, height: 300),
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: true)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = false
        isReleasedWhenClosed = false
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        animationBehavior = .utilityWindow
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// ドラッグ開始の判定用。押した位置（ウィンドウ座標）。スクロールバーの上で押したときは nil
    private var dragStart: NSPoint?

    override func sendEvent(_ event: NSEvent) {
        if event.type == .keyDown, let handler = keyHandler, handler(event) { return }
        // パネルのどこでもドラッグで移動できるようにする（クリックは 4pt 動くまで通常どおり扱う）
        switch event.type {
        case .leftMouseDown:
            let hit = contentView?.hitTest(event.locationInWindow)
            dragStart = (hit is NSScroller) ? nil : event.locationInWindow
        case .leftMouseDragged:
            if let s = dragStart {
                let p = event.locationInWindow
                if hypot(p.x - s.x, p.y - s.y) > 4 {
                    dragStart = nil
                    performDrag(with: event)
                    return
                }
            }
        case .leftMouseUp:
            dragStart = nil
        default:
            break
        }
        super.sendEvent(event)
    }

    override func cancelOperation(_ sender: Any?) {
        orderOut(nil)
    }
}

@MainActor
final class PanelController: NSObject, NSWindowDelegate {
    let model: PanelModel
    let panel = TrendPanel()
    private let effect = NSVisualEffectView()
    private let hosting: NSHostingView<PanelRootView>
    /// 最新日を表示したときに呼ぶ（既読にする）
    var onShowLatest: ((String) -> Void)?
    /// URL を開く処理（テストで差し替える）
    var openURL: (URL) -> Void = { NSWorkspace.shared.open($0) }
    /// C キーのコピー先（テストで差し替える）
    var pasteboard: NSPasteboard = .general
    /// 直近のレイアウトで測った内容の高さ（pt）と、パネルの高さの上限（pt）
    private(set) var lastMeasuredHeight: CGFloat = 0
    private(set) var lastMaxHeight: CGFloat = 0

    static let lineStep: CGFloat = 48

    init(model: PanelModel) {
        self.model = model
        hosting = NSHostingView(rootView: PanelRootView(model: model))
        super.init()

        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.maskImage = Self.roundedMask(radius: 14)
        effect.frame = NSRect(x: 0, y: 0, width: Style.panelWidth, height: 300)
        effect.autoresizingMask = [.width, .height]

        hosting.sizingOptions = []
        hosting.frame = effect.bounds
        hosting.autoresizingMask = [.width, .height]
        effect.addSubview(hosting)

        panel.contentView = effect
        panel.delegate = self
        panel.keyHandler = { [weak self] event in
            guard let self else { return false }
            return self.handleKey(event)
        }
        // マウス・トラックパッドでのスクロールも含め、スクロール位置が変わったら「続きあり」を更新する
        NotificationCenter.default.addObserver(self, selector: #selector(clipBoundsChanged(_:)),
                                               name: NSView.boundsDidChangeNotification, object: nil)
    }

    @objc private func clipBoundsChanged(_ note: Notification) {
        guard let clip = note.object as? NSClipView, clip.window === panel else { return }
        updateMoreBelow()
    }

    /// 本文の下にまだ見えていない内容があるかを model に反映する
    func updateMoreBelow() {
        var more = false
        if model.overflows, let sv = scrollView, let doc = sv.documentView, let off = scrollOffsetFromTop {
            let maxOffset = doc.frame.height - sv.contentView.bounds.height
            more = off < maxOffset - 4
        }
        if model.moreBelow != more { model.moreBelow = more }
    }

    var isVisible: Bool { panel.isVisible }

    func toggle() {
        if panel.isVisible { close(reason: "hotkey toggle") } else { show() }
    }

    func show() {
        model.reloadLatest()
        layout(keepTop: false)
        panel.orderFrontRegardless()
        panel.makeKey()
        noteIfLatest()
        ActivityLog.write("panel shown on \"\(panel.screen?.localizedName ?? "?")\" frame=\(NSStringFromRect(panel.frame)) "
                          + "visible=\(panel.isVisible) key=\(panel.isKeyWindow) date=\(model.currentDate ?? "-")")
    }

    func close(reason: String = "toggle") {
        guard panel.isVisible else { return }
        noteUserMove()
        panel.orderOut(nil)
        ActivityLog.write("panel closed (\(reason))")
    }

    /// デバッグ用: 画面に出さずに読込とレイアウトだけ行う
    func layoutForDebug() {
        model.reloadLatest()
        layout(keepTop: false)
    }

    var hostingView: NSView { hosting }

    func windowDidResignKey(_ notification: Notification) {
        close(reason: "lost focus")
    }

    /// 直近のレイアウトで置いたパネルの左上。ここから動いていたら、ユーザーがドラッグで動かしたとみなす
    private var lastTopLeft: NSPoint?

    /// ドラッグで動かされていたら、その位置を保存する
    private func noteUserMove() {
        guard panel.isVisible, let last = lastTopLeft else { return }
        let now = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
        guard abs(now.x - last.x) > 0.5 || abs(now.y - last.y) > 0.5 else { return }
        PanelPositionStore.save(now)
        lastTopLeft = now
        ActivityLog.write("panel moved by drag; saved top-left=\(NSStringFromPoint(now))")
    }

    private func noteIfLatest() {
        if model.isShowingLatest, let d = model.currentDate { onShowLatest?(d) }
    }

    // 高さを内容に合わせる（画面の 80% が上限、超えたら本文だけスクロール）
    private func layout(keepTop: Bool) {
        noteUserMove()
        // ドラッグで動かした位置があれば、その画面のその位置に出す（画面が無くなっていたら既定の位置）
        let saved = PanelPositionStore.topLeft
        let savedScreen = saved.flatMap { p in NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } }
        let screen = (keepTop ? panel.screen : nil) ?? savedScreen ?? Self.mouseScreen()
        let vf = screen.visibleFrame
        let width = Style.panelWidth
        let natural = Self.measure(model.state, width: width)
        let maxHeight = floor(vf.height * 0.8)
        let height = min(natural, maxHeight)
        lastMeasuredHeight = natural
        lastMaxHeight = maxHeight
        model.overflows = natural > maxHeight
        var x = vf.midX - width / 2
        var y: CGFloat
        if keepTop && panel.isVisible {
            x = panel.frame.minX
            y = panel.frame.maxY - height
        } else if let saved, savedScreen != nil {
            x = min(max(saved.x, vf.minX + 12), vf.maxX - width - 12)
            y = saved.y - height
        } else {
            let centerY = vf.midY + vf.height * 0.12   // 中央やや上
            y = centerY - height / 2
        }
        y = min(max(y, vf.minY + 12), vf.maxY - height - 12)
        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
        lastTopLeft = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
        panel.invalidateShadow()
        hosting.layoutSubtreeIfNeeded()
        updateMoreBelow()
        // SwiftUI がスクロールビューを作り直した後にもう一度確かめる
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.updateMoreBelow() }
        }
    }

    static func measure(_ state: DisplayState, width: CGFloat) -> CGFloat {
        let probe = NSHostingView(rootView: TrendContentView(state: state).frame(width: width))
        return ceil(probe.fittingSize.height) + 1
    }

    static func mouseScreen() -> NSScreen {
        let p = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
    }

    static func roundedMask(radius: CGFloat) -> NSImage {
        let edge = radius * 2 + 1
        let image = NSImage(size: NSSize(width: edge, height: edge), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }

    // MARK: スクロール（SwiftUI の ScrollView の中の NSScrollView を直接動かす）

    enum ScrollTarget {
        case by(CGFloat)
        case page(Int)
        case top
        case bottom
    }

    /// 本文のスクロールビュー。日付を移ると作り直されるので毎回探す。
    var scrollView: NSScrollView? {
        func find(_ v: NSView) -> NSScrollView? {
            if let s = v as? NSScrollView { return s }
            for sub in v.subviews {
                if let s = find(sub) { return s }
            }
            return nil
        }
        return find(hosting)
    }

    /// 上端からのスクロール量（pt）。スクロールビューが無ければ nil。
    var scrollOffsetFromTop: CGFloat? {
        guard let sv = scrollView, let doc = sv.documentView else { return nil }
        let visible = sv.contentView.bounds
        return doc.isFlipped ? visible.minY - doc.frame.minY : doc.frame.maxY - visible.maxY
    }

    /// 戻り値: 位置が変わったか
    @discardableResult
    func scroll(_ target: ScrollTarget) -> Bool {
        guard let sv = scrollView, let doc = sv.documentView, let current = scrollOffsetFromTop else { return false }
        let clip = sv.contentView
        let visible = clip.bounds
        let maxOffset = max(0, doc.frame.height - visible.height)
        var next: CGFloat
        switch target {
        case .by(let d): next = current + d
        case .page(let dir): next = current + CGFloat(dir) * max(Self.lineStep, visible.height - Self.lineStep)
        case .top: next = 0
        case .bottom: next = maxOffset
        }
        next = min(max(0, next), maxOffset)
        guard abs(next - current) > 0.5 else { return false }
        let y = doc.isFlipped ? doc.frame.minY + next : doc.frame.maxY - visible.height - next
        let proposed = NSRect(x: visible.minX, y: y, width: visible.width, height: visible.height)
        clip.scroll(to: clip.constrainBoundsRect(proposed).origin)
        sv.reflectScrolledClipView(clip)
        updateMoreBelow()
        return true
    }

    // MARK: キー操作

    private func handleKey(_ event: NSEvent) -> Bool {
        let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            .subtracting([.capsLock, .numericPad, .function])
        let ch = event.charactersIgnoringModifiers?.lowercased() ?? ""
        let code = Int(event.keyCode)

        if code == kVK_Escape {
            close(reason: "esc")
            return true
        }
        if mods == [.command] {
            if ch == "w" || code == kVK_ANSI_W { close(reason: "cmd+w"); return true }
            if ch == "c" || code == kVK_ANSI_C { copyCitation(); return true }
            if code == kVK_UpArrow { scroll(.top); return true }
            if code == kVK_DownArrow { scroll(.bottom); return true }
            return false
        }
        guard mods.isEmpty || mods == [.shift] else { return false }

        switch code {
        case kVK_LeftArrow:
            step(-1)
            return true
        case kVK_RightArrow:
            step(+1)
            return true
        case kVK_DownArrow:
            scroll(.by(Self.lineStep))
            return true
        case kVK_UpArrow:
            scroll(.by(-Self.lineStep))
            return true
        case kVK_Space:
            scroll(.page(mods == [.shift] ? -1 : 1))
            return true
        case kVK_PageDown:
            scroll(.page(1))
            return true
        case kVK_PageUp:
            scroll(.page(-1))
            return true
        case kVK_Home:
            scroll(.top)
            return true
        case kVK_End:
            scroll(.bottom)
            return true
        default:
            break
        }
        if code == kVK_ANSI_O || ch == "o" {
            open(model.state.article.flatMap { safeURL($0.source.url) })
            return true
        }
        if code == kVK_ANSI_D || ch == "d" {
            open(model.state.article?.firstBuzzURL)
            return true
        }
        if code == kVK_ANSI_C || ch == "c" {
            copyCitation()
            return true
        }
        return false
    }

    private func step(_ delta: Int) {
        if model.move(delta) {
            layout(keepTop: true)
            noteIfLatest()
        } else {
            NSSound.beep()
        }
    }

    private func open(_ url: URL?) {
        guard let url else { NSSound.beep(); return }
        openURL(url)
        close(reason: "opened link")
    }

    private func copyCitation() {
        guard let a = model.state.article else { NSSound.beep(); return }
        pasteboard.clearContents()
        pasteboard.setString(a.citationMarkdown, forType: .string)
        model.flash("引用元を Markdown でコピーしました")
    }
}

// MARK: - メニューバーアイコン

enum StatusIcon {
    static func image(unread: Bool, generating: Bool) -> NSImage? {
        let name = generating ? "arrow.triangle.2.circlepath" : "newspaper"
        guard let sym = NSImage(systemSymbolName: name, accessibilityDescription: "TrendBar"),
              let base = sym.withSymbolConfiguration(.init(pointSize: 14, weight: .regular)) else { return nil }
        if !unread || generating {
            base.isTemplate = true
            return base
        }
        // 未読: 右上に小さい点
        let bs = base.size
        let dot: CGFloat = 6
        let size = NSSize(width: ceil(bs.width + 3), height: ceil(max(bs.height, 16)))
        let img = NSImage(size: size, flipped: false) { rect in
            base.draw(in: NSRect(x: 0, y: (rect.height - bs.height) / 2, width: bs.width, height: bs.height))
            let dr = NSRect(x: rect.maxX - dot, y: rect.maxY - dot, width: dot, height: dot)
            if let ctx = NSGraphicsContext.current {
                ctx.compositingOperation = .clear
                NSBezierPath(ovalIn: dr.insetBy(dx: -1.5, dy: -1.5)).fill()
                ctx.compositingOperation = .sourceOver
            }
            NSColor.black.setFill()
            NSBezierPath(ovalIn: dr).fill()
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "TrendBar（未読あり）"
        return img
    }
}

// MARK: - アプリ本体

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let store = DataStore.resolve()
    private lazy var model = PanelModel(store: store)
    private lazy var panelController = PanelController(model: model)
    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private let hotKeys = HotKeyManager()
    private let hotKeyConfig = ConfigLoader.hotKey()
    private var hotKeyStatus: OSStatus = noErr
    private var generating = false
    private var generateNote: String?
    private var generateLog: FileHandle?
    private var timer: Timer?
    private var wakeObserver: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        if Self.otherInstanceRunning() {
            NSApp.terminate(nil)
            return
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu

        panelController.onShowLatest = { [weak self] date in
            SeenStore.markSeen(date)
            self?.refreshIcon()
        }

        hotKeys.onPress = { [weak self] in
            ActivityLog.write("hotkey pressed")
            MainActor.assumeIsolated { self?.panelController.toggle() }
        }
        let handlerStatus = hotKeys.installHandler()
        hotKeyStatus = handlerStatus == noErr ? hotKeys.register(hotKeyConfig.spec) : handlerStatus
        ActivityLog.write("launched; hotkey register status=\(hotKeyStatus)")

        refreshIcon()

        let t = Timer(timeInterval: 300, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshIcon() }
        }
        t.tolerance = 30
        RunLoop.main.add(t, forMode: .common)
        timer = t

        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshIcon() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotKeys.unregister()
        hotKeys.removeHandler()
    }

    private static func otherInstanceRunning() -> Bool {
        guard let id = Bundle.main.bundleIdentifier else { return false }
        let me = ProcessInfo.processInfo.processIdentifier
        return NSRunningApplication.runningApplications(withBundleIdentifier: id).contains {
            $0.processIdentifier != me && $0.activationPolicy == .accessory && !$0.isTerminated
        }
    }

    private var hotKeyOK: Bool { hotKeyStatus == noErr }

    private func refreshIcon() {
        guard let button = statusItem?.button else { return }
        let latest = store.availableDates().last
        let unread = SeenStore.isUnread(latest: latest)
        if let img = StatusIcon.image(unread: unread, generating: generating) {
            button.image = img
            button.title = ""
        } else {
            button.image = nil
            button.title = unread ? "T•" : "T"
        }
        var tip = "TrendBar"
        if let latest { tip += " — 最新 \(latest)" + (unread ? "（未読）" : "") }
        if generating { tip += " — 生成中…" }
        button.toolTip = tip
    }

    // MARK: メニュー

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let latest = store.availableDates().last

        if !hotKeyOK {
            addInfo("ホットキー登録失敗: 設定を変えてください")
            addInfo("  \(hotKeyConfig.spec.display) — \(HotKeyManager.describe(hotKeyStatus))")
        }
        if let note = hotKeyConfig.note { addInfo(note) }
        if !hotKeyOK || hotKeyConfig.note != nil { menu.addItem(.separator()) }

        let showTitle = hotKeyOK ? "今日のトレンドを表示 (\(hotKeyConfig.spec.display))" : "今日のトレンドを表示"
        addAction(showTitle, #selector(showPanel))
        let openItem = addAction("元記事を開く", #selector(openSource))
        openItem.isEnabled = latest != nil
        if let latest {
            addInfo("最新: \(latest)" + (SeenStore.isUnread(latest: latest) ? "（未読）" : ""))
        }

        menu.addItem(.separator())
        let gen = addAction(generating ? "生成中…" : "今すぐ生成／再生成", #selector(generateNow))
        gen.isEnabled = !generating
        // 種類を絞って作り直す（その回だけ。毎朝の既定は config.toml の [pick] focus）
        let focusItem = NSMenuItem(title: "種類を指定して生成", action: nil, keyEquivalent: "")
        let focusMenu = NSMenu()
        for (title, focus) in Self.focusChoices {
            let item = NSMenuItem(title: title, action: #selector(generateWithFocus(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = focus
            focusMenu.addItem(item)
        }
        focusItem.submenu = focusMenu
        focusItem.isEnabled = !generating
        menu.addItem(focusItem)
        if let note = generateNote { addInfo(note) }
        addAction("データフォルダを開く", #selector(openDataFolder))
        if PanelPositionStore.topLeft != nil {
            addAction("パネルの位置を元に戻す", #selector(resetPanelPosition))
        }

        menu.addItem(.separator())
        addAction("終了", #selector(quit), key: "q")
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshIcon()
    }

    @discardableResult
    private func addAction(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        menu.addItem(item)
        return item
    }

    private func addInfo(_ title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        menu.addItem(item)
    }

    @objc private func showPanel() {
        // メニューが閉じてから出す
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.panelController.show() }
        }
    }

    @objc private func resetPanelPosition() {
        PanelPositionStore.clear()
    }

    @objc private func openSource() {
        guard let d = store.availableDates().last,
              case .success(let a) = store.load(date: d),
              let url = safeURL(a.source.url) else {
            NSSound.beep()
            return
        }
        NSWorkspace.shared.open(url)
        // 元記事を開いたら、その日は読んだものとして未読の印を消す
        SeenStore.markSeen(d)
        refreshIcon()
    }

    @objc private func openDataFolder() {
        var url = store.dir
        let fm = FileManager.default
        while !fm.fileExists(atPath: url.path) && url.path != "/" {
            url.deleteLastPathComponent()
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: 生成（bin/trend run --force）

    /// 「種類を指定して生成」の選択肢（表示名, trend run --focus の値。daily_trend/rank.py の FOCUS_PRESETS と同じ）
    static let focusChoices: [(String, String)] = [
        ("おまかせ（すべての種類）", "balanced"),
        ("ニュース・発表", "news"),
        ("テックブログ", "blogs"),
        ("OSS・公開モデル", "oss"),
        ("論文", "papers"),
    ]

    /// trend run が「別の trend run が実行中」で何もしなかったときの終了コード（daily_trend/cli.py の EXIT_BUSY）
    static let exitBusy: Int32 = 75

    @objc private func generateNow() {
        startGeneration(focus: nil)
    }

    @objc private func generateWithFocus(_ sender: NSMenuItem) {
        guard let focus = sender.representedObject as? String, Self.focusChoices.contains(where: { $0.1 == focus }) else { return }
        startGeneration(focus: focus)
    }

    /// focus が nil なら config.toml の [pick] focus に従う
    private func startGeneration(focus: String?) {
        guard !generating else { return }
        guard FileManager.default.isExecutableFile(atPath: Paths.trendCommand.path) else {
            generateNote = "trend コマンドが見つかりません（~/daily-trend/bin/trend）"
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        let command = "trend run --force" + (focus.map { " --focus \($0)" } ?? "")
        p.arguments = ["-c", "$HOME/daily-trend/bin/" + command]
        p.standardInput = FileHandle.nullDevice
        let log = openGenerateLog(command: command)
        p.standardOutput = log ?? FileHandle.nullDevice
        p.standardError = log ?? FileHandle.nullDevice
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.generationFinished(code: code) }
            }
        }
        do {
            try p.run()
            generating = true
            generateLog = log
            generateNote = nil
            refreshIcon()
        } catch {
            try? log?.close()
            generateNote = "生成を開始できません: \(error.localizedDescription)"
        }
    }

    private func openGenerateLog(command: String) -> FileHandle? {
        let fm = FileManager.default
        let dir = Paths.logsDir
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("trendbar-generate.log")
        if !fm.fileExists(atPath: url.path) { fm.createFile(atPath: url.path, contents: nil) }
        guard let fh = try? FileHandle(forWritingTo: url) else { return nil }
        fh.seekToEndOfFile()
        let header = "\n==== \(DateUtil.fullFormatter.string(from: Date())) TrendBar: \(command) ====\n"
        fh.write(Data(header.utf8))
        return fh
    }

    private func generationFinished(code: Int32) {
        generating = false
        try? generateLog?.close()
        generateLog = nil
        let now = DateUtil.timeFormatter.string(from: Date())
        if code == 0 {
            generateNote = "前回の生成: 成功（\(now)）"
        } else if code == Self.exitBusy {
            generateNote = "前回の生成: 別の trend run が実行中のため行いませんでした（\(now)）。終わってから再度お試しください"
        } else {
            generateNote = "前回の生成: 失敗（終了コード \(code)、\(now)）ログ: data/logs/trendbar-generate.log"
        }
        refreshIcon()
        if panelController.isVisible { panelController.show() }
    }
}

// MARK: - セルフテスト用 CLI

@MainActor
enum CLI {
    static let usage = """
    使い方:
      TrendBar                      メニューバーに常駐
      TrendBar --dump               最新データの読込結果を表示
      TrendBar --check-hotkey       ホットキー登録を試して結果を表示（すぐ解除）
      TrendBar --render-png OUT.png [--date YYYY-MM-DD] [--appearance light|dark]
                                    パネルの中身を画面に出さずに PNG に描画
      TrendBar --render-icon OUT.png [--unread]      メニューバーアイコンを PNG に描画
      TrendBar --debug-panel OUT.png [dark] [end]    実パネルを画面に出さずにレイアウトして PNG に（end: 末尾までスクロール）
    環境変数: DAILY_TREND_HOME（data/ と config.toml の親。未設定なら ~/.zshenv の値、無ければ ~/daily-trend）
              DAILY_TREND_DATA（データフォルダ）, DAILY_TREND_CONFIG（config.toml）
    """

    static func prepareApp() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
    }

    static func dump() -> Int32 {
        prepareApp()
        let store = DataStore.resolve()
        let rootNote = Paths.projectRoot.standardizedFileURL == Paths.codeRoot.standardizedFileURL ? "既定" : "DAILY_TREND_HOME"
        print("data_dir: \(store.dir.path)" + (store.fromEnv ? "  (DAILY_TREND_DATA)" : "  (\(rootNote))"))
        let dates = store.availableDates()
        guard let latest = dates.last else {
            print("files: 0")
            print("result: データなし（まだ生成されていません）")
            return 3
        }
        print("files: \(dates.count)  (\(dates.first!) .. \(latest))")
        print("latest_file: \(store.fileURL(for: latest).path)")
        switch store.load(date: latest) {
        case .failure(let e):
            print("decode: 失敗 — \(e.message)")
            return 1
        case .success(let a):
            let bad = a.unverifiedPointIndices.sorted()
            print("decode: 成功")
            print("schema_version: \(a.schemaVersion)")
            print("date: \(a.date)")
            print("category: \(a.category ?? "-")")
            print("headline: \(a.headline)")
            print("points: \(a.points.count)" + (bad.isEmpty ? "" : "  (⚠︎ 根拠未確認: 要点 \(bad.map { String($0 + 1) }.joined(separator: ",")))"))
            print("why_now: \(a.whyNow.nonEmpty == nil ? "なし" : (a.informativeWhyNow == nil ? "あり（本文からは不明 → 表示しない）" : "あり"))")
            print("terms: \(a.visibleTerms.count)")
            print("source: \(a.source.title ?? "-") <\(a.source.url)>")
            print("buzz: \(a.buzz.count)")
            let g = a.warningGroups
            print("warnings: \(a.displayWarnings.count)  (見出し \(g.headline.count), 結論 \(g.oneLiner.count), 要点別 \(g.byPoint.values.map(\.count).reduce(0, +)), その他 \(g.general.count))")
            print("buzz_url_for_D: \(a.firstBuzzURL?.absoluteString ?? "なし")")
            print("unread: \(SeenStore.isUnread(latest: latest) ? "yes" : "no")  (last_seen: \(SeenStore.lastSeen ?? "なし"))")
            print("citation_markdown:")
            print(a.citationMarkdown)
            return 0
        }
    }

    static func checkHotKey() -> Int32 {
        prepareApp()
        let cfg = ConfigLoader.hotKey()
        print("config: \(Paths.configFile.path)" + (FileManager.default.fileExists(atPath: Paths.configFile.path) ? "" : "（無い）"))
        print("hotkey: \(cfg.spec.display)  [\(cfg.spec.raw)]  keyCode=\(cfg.spec.keyCode) modifiers=0x\(String(cfg.spec.carbonModifiers, radix: 16))")
        print("source: \(cfg.source)")
        if let note = cfg.note { print("note: \(note)") }
        let mgr = HotKeyManager()
        let hs = mgr.installHandler()
        print("InstallEventHandler: \(hs)")
        let st = hs == noErr ? mgr.register(cfg.spec) : hs
        print("RegisterEventHotKey(exclusive): \(st) \(HotKeyManager.describe(st))")
        print("注意: 他のアプリが同じキーを通常登録（非 exclusive）している場合は検出できません")
        mgr.unregister()
        mgr.removeHandler()
        print("unregistered: yes")
        return st == noErr ? 0 : 1
    }

    static func renderPNG(_ args: [String]) -> Int32 {
        prepareApp()
        guard let out = args.first, !out.hasPrefix("--") else {
            fputs("--render-png には出力先が必要です\n\(usage)\n", stderr)
            return 2
        }
        var date: String?
        var dark = false
        var i = 1
        while i < args.count {
            switch args[i] {
            case "--date":
                guard i + 1 < args.count else { fputs("--date に値がありません\n", stderr); return 2 }
                date = args[i + 1]; i += 2
            case "--appearance":
                guard i + 1 < args.count else { fputs("--appearance に値がありません\n", stderr); return 2 }
                let v = args[i + 1].lowercased()
                guard v == "light" || v == "dark" else { fputs("--appearance は light か dark\n", stderr); return 2 }
                dark = v == "dark"; i += 2
            default:
                fputs("不明な引数: \(args[i])\n", stderr); return 2
            }
        }

        let store = DataStore.resolve()
        let model = PanelModel(store: store)
        if let date {
            guard model.select(date: date) else {
                fputs("\(date).json が \(store.dir.path) にありません\n", stderr)
                return 2
            }
        } else {
            model.reloadLatest()
        }
        let state = model.state

        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)!
        NSApp.appearance = appearance
        var cgImage: CGImage?
        var measured: CGFloat = 0
        appearance.performAsCurrentDrawingAppearance {
            measured = PanelController.measure(state, width: Style.panelWidth)
            let backdrop = dark ? Color(red: 0.36, green: 0.37, blue: 0.40) : Color(red: 0.86, green: 0.87, blue: 0.89)
            let view = TrendContentView(state: state)
                .frame(width: Style.panelWidth)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color(nsColor: .windowBackgroundColor))
                )
                .padding(24)
                .background(backdrop)
                .environment(\.colorScheme, dark ? .dark : .light)
            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            cgImage = renderer.cgImage
        }
        guard let cg = cgImage else {
            fputs("描画に失敗しました\n", stderr)
            return 1
        }
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            fputs("PNG 変換に失敗しました\n", stderr)
            return 1
        }
        let url = URL(fileURLWithPath: (out as NSString).expandingTildeInPath)
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try png.write(to: url)
        } catch {
            fputs("書き込みに失敗しました: \(error.localizedDescription)\n", stderr)
            return 1
        }
        let screenH = NSScreen.main.map { $0.visibleFrame.height } ?? 0
        print("wrote: \(url.path)  (\(cg.width)x\(cg.height)px @2x, appearance=\(dark ? "dark" : "light"))")
        print("date: \(state.date ?? "-")  content: \(describe(state.content))")
        print("panel_height_pt: \(Int(measured))  (NSHostingView.fittingSize, width \(Int(Style.panelWidth))pt)"
              + (screenH > 0 ? "  screen_80%: \(Int(screenH * 0.8))pt → \(measured > screenH * 0.8 ? "スクロール" : "収まる")" : ""))
        return 0
    }

    static func renderIcon(_ args: [String]) -> Int32 {
        prepareApp()
        guard let out = args.first else { return 2 }
        let unread = args.contains("--unread")
        guard let img = StatusIcon.image(unread: unread, generating: false) else { return 1 }
        let scale: CGFloat = 4
        let size = NSSize(width: img.size.width * scale, height: img.size.height * scale)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return 1 }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.white.setFill()
        NSRect(origin: .zero, size: size).fill()
        img.draw(in: NSRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
        do { try png.write(to: URL(fileURLWithPath: out)) } catch { return 1 }
        print("wrote: \(out)  (\(unread ? "unread" : "read"))")
        return 0
    }

    /// 実際のパネル（NSPanel + NSHostingView + ScrollView）を画面に出さずにレイアウトし、中身を PNG にする
    static func debugPanel(_ args: [String]) -> Int32 {
        prepareApp()
        guard let out = args.first else { return 2 }
        let dark = args.contains("dark")
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let pc = PanelController(model: PanelModel(store: DataStore.resolve()))
        pc.layoutForDebug()
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        if args.contains("end") {
            // 本文を末尾までスクロールしてから描画（「続きあり」が消えるかの確認用）
            pc.hostingView.layoutSubtreeIfNeeded()
            pc.scroll(.bottom)
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        }
        let view = pc.hostingView
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return 1 }
        view.cacheDisplay(in: view.bounds, to: rep)
        // 実パネルは NSVisualEffectView の上に載る。確認用に不透明の背景色の上へ合成する。
        let size = view.bounds.size
        guard let out2 = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: rep.pixelsWide, pixelsHigh: rep.pixelsHigh,
                                          bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                          colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return 1 }
        out2.size = size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out2)
        NSApp.effectiveAppearance.performAsCurrentDrawingAppearance {
            NSColor.windowBackgroundColor.setFill()
            NSRect(origin: .zero, size: size).fill()
        }
        rep.draw(in: NSRect(origin: .zero, size: size), from: .zero, operation: .sourceOver, fraction: 1,
                 respectFlipped: true, hints: nil)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = out2.representation(using: .png, properties: [:]) else { return 1 }
        do { try png.write(to: URL(fileURLWithPath: out)) } catch { return 1 }
        let f = pc.panel.frame
        let vf = PanelController.mouseScreen().visibleFrame
        print("panel_frame: x=\(Int(f.minX)) y=\(Int(f.minY)) w=\(Int(f.width)) h=\(Int(f.height))  visible=\(pc.panel.isVisible)")
        print("screen_visible: w=\(Int(vf.width)) h=\(Int(vf.height))  max_h(80%)=\(Int(floor(vf.height * 0.8)))")
        print("hosting_bounds: \(Int(view.bounds.width))x\(Int(view.bounds.height))")
        print("content_h: \(Int(pc.lastMeasuredHeight))  overflows: \(pc.model.overflows)  more_below: \(pc.model.moreBelow)  scroll_offset: \(pc.scrollOffsetFromTop.map { String(Int($0)) } ?? "-")")
        print("wrote: \(out)")
        return 0
    }

    private static func describe(_ c: DisplayState.Content) -> String {
        switch c {
        case .empty: return "empty"
        case .failed(_, let m): return "failed (\(m))"
        case .article(let a): return "article (points \(a.points.count), buzz \(a.buzz.count))"
        }
    }
}

// MARK: - エントリポイント

@main
enum TrendBarMain {
    static var appDelegate: AppDelegate?

    @MainActor
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if let first = args.first, first.hasPrefix("--") {
            let rest = Array(args.dropFirst())
            switch first {
            case "--render-png": exit(CLI.renderPNG(rest))
            case "--check-hotkey": exit(CLI.checkHotKey())
            case "--dump": exit(CLI.dump())
            case "--render-icon": exit(CLI.renderIcon(rest))
            case "--debug-panel": exit(CLI.debugPanel(rest))
            case "--help": print(CLI.usage); exit(0)
            default:
                fputs("不明なオプション: \(first)\n\(CLI.usage)\n", stderr)
                exit(2)
            }
        }
        // 引数なし（または macOS が付ける -psn_ 等）: 常駐起動
        let app = NSApplication.shared
        let delegate = AppDelegate()
        appDelegate = delegate
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
