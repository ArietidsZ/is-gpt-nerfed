import Foundation

/// Interface copy, including the presentation of backend messages. Stored records and user content stay untouched.
enum L10n {
    private final class BundleFinder {}

    /// The SwiftPM resource bundle. Not `Bundle.module`: the accessor SwiftPM's native build system generates only
    /// looks at the .app root and at the absolute `.build` path of the machine that built it, so a release built
    /// that way traps on every other machine (#2). A missing bundle degrades to `Bundle.main`, whose lookups return
    /// the keys, which are the English strings.
    static let resources: Bundle = {
        let name = "IsGPTNerfed_IsGPTNerfed.bundle"
        let finder = Bundle(for: BundleFinder.self)
        let dirs = [Bundle.main.resourceURL, Bundle.main.bundleURL,                  // .app: Contents/Resources, or root
                    finder.resourceURL, finder.bundleURL.deletingLastPathComponent()] // tests: next to the .xctest
        for dir in dirs {
            guard let url = dir?.appendingPathComponent(name) else { continue }
            if FileManager.default.fileExists(atPath: url.path), let bundle = Bundle(url: url) { return bundle }
        }
        return .main
    }()

    private static let englishBundle: Bundle = {
        guard let path = resources.path(forResource: "en", ofType: "lproj"),
              let bundle = Bundle(path: path) else { return resources }
        return bundle
    }()

    static func tr(_ key: String, _ arguments: CVarArg...) -> String {
        format(key, bundle: resources, arguments: arguments)
    }

    /// Explicit-language lookup keeps localization behavior testable and falls back to the package's English table.
    static func tr(_ key: String, language: String?, arguments: [CVarArg] = []) -> String {
        let bundle = language.map { resourceBundle(for: $0) ?? englishBundle } ?? resources
        return format(key, bundle: bundle, arguments: arguments)
    }

    static func resourceBundle(for language: String) -> Bundle? {
        // SwiftPM lowercases .lproj directory names in the built resource bundle (zh-Hans → zh-hans.lproj), and
        // Bundle.path(forResource:ofType:) matches exactly, so look the directory up case-insensitively.
        let wanted = language.lowercased()
        let path = resources.paths(forResourcesOfType: "lproj", inDirectory: nil)
            .first { ($0 as NSString).lastPathComponent.lowercased() == wanted + ".lproj" }
        guard let path else { return nil }
        return Bundle(path: path)
    }

    private static func format(_ key: String, bundle: Bundle, arguments: [CVarArg]) -> String {
        let format = bundle.localizedString(forKey: key, value: key, table: "Localizable")
        let englishFormat = format == key
            ? englishBundle.localizedString(forKey: key, value: key, table: "Localizable")
            : format
        guard !arguments.isEmpty else { return englishFormat }
        let locale = Locale(identifier: bundle.preferredLocalizations.first ?? Locale.current.identifier)
        return String(format: englishFormat, locale: locale, arguments: arguments)
    }

    /// The CLI's compact age strings are display metadata, not evidence. Translate known forms and preserve unknown ones.
    static func ago(_ value: String?) -> String? {
        localizedAgo(value, language: nil)
    }

    static func localizedAgo(_ value: String?, language: String?) -> String? {
        guard let value, !value.isEmpty else { return value }
        let pattern = #"^(\d+)([smhd]) ago$"#
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)),
              let countRange = Range(match.range(at: 1), in: value),
              let unitRange = Range(match.range(at: 2), in: value) else { return value }
        let count = String(value[countRange])
        let key: String
        switch value[unitRange] {
        case "s": key = "%@s ago"
        case "m": key = "%@m ago"
        case "h": key = "%@h ago"
        case "d": key = "%@d ago"
        default: return value
        }
        return tr(key, language: language, arguments: [count])
    }

    /// Translate only known CLI summary components; an unknown component keeps the complete original message.
    static func statusMessage(_ message: String, language: String? = nil) -> String {
        let components = message.components(separatedBy: " · ")
        let translated = components.compactMap { statusComponent($0, language: language) }
        guard translated.count == components.count else { return message }
        return translated.joined(separator: " · ")
    }

    private static func statusComponent(_ value: String, language: String?) -> String? {
        switch value {
        case "fresh session downgraded", "all clear", "no active threads":
            return tr(value, language: language)
        default:
            break
        }

        let parts = value.split(separator: " ", maxSplits: 1).map(String.init)
        guard parts.count == 2, Int(parts[0]) != nil else { return nil }
        let count = parts[0]
        switch parts[1] {
        case "downgraded": return tr("%@ downgraded", language: language, arguments: [count])
        case "suspicious": return tr("%@ suspicious", language: language, arguments: [count])
        case "upgraded": return tr("%@ upgraded", language: language, arguments: [count])
        case "unverified": return tr("%@ unverified", language: language, arguments: [count])
        case "probe running": return tr("%@ probe running", language: language, arguments: [count])
        case "probes running": return tr("%@ probes running", language: language, arguments: [count])
        default: return nil
        }
    }

    static func evidenceText(_ text: String, ago: String?, active: Bool, expected: Bool = false, language: String? = nil) -> String {
        var result = backend(text, language: language)
        if let ago = localizedAgo(ago, language: language), !ago.isEmpty { result += " · \(ago)" }
        if expected { result += " · \(tr("Expected by you", language: language))" }
        else if !active { result += " · \(tr("reverted", language: language))" }
        return result
    }

    /// Translate only titles supplied by the app itself, never a user's real session title.
    static func sessionTitle(_ title: String, isDemo: Bool, isHidden: Bool, language: String? = nil) -> String {
        if isHidden, title.hasPrefix("Session "), Int(title.dropFirst(8)) != nil {
            return tr("Session %@", language: language, arguments: [String(title.dropFirst(8))])
        }
        if isDemo { return tr(title, language: language) }
        return title
    }

    static func frequency(_ value: String, active: Bool, language: String? = nil) -> String {
        if value == "manual" { return tr("Manually", language: language) }
        if value.hasPrefix("turns:"), let count = Int(value.dropFirst(6)), count > 0 {
            return tr("Every %@ turns", language: language, arguments: [String(count)])
        }
        guard let unit = value.last, let count = Int(value.dropLast()), count > 0 else { return value }
        switch unit {
        case "m":
            return tr(active ? "Every %@ min of activity" : "Every %@ minutes", language: language, arguments: [String(count)])
        case "h":
            if count == 1 { return tr(active ? "Every hour of activity" : "Every hour", language: language) }
            return tr(active ? "Every %@ hours of activity" : "Every %@ hours", language: language, arguments: [String(count)])
        default:
            return value
        }
    }

    /// Keep the report's user-supplied title separate from generated diagnostic prose.
    static func report(_ text: String, title: String, displayTitle: String, language: String? = nil) -> String {
        let lines = text.components(separatedBy: "\n")
        let prefix = "is-gpt-nerfed · \(title) · "
        guard let first = lines.first, first.hasPrefix(prefix) else { return text }
        let header = "is-gpt-nerfed · \(displayTitle) · " + first.dropFirst(prefix.count)
        guard lines.count > 1 else { return header }
        return header + "\n" + backend(lines.dropFirst().joined(separator: "\n"), language: language)
    }
}
