import AppKit
import Foundation
import Observation
import OSLog
import ServiceManagement

/// Unified-logging channel for the menu bar app (Console.app: subsystem "is-gpt-nerfed"). The ledger's own
/// activity log (`nerfed log`) is the primary record; this only covers what happens inside the app.
let appLog = Logger(subsystem: "is-gpt-nerfed", category: "app")

@MainActor
@Observable
final class Store {
    static let shared = Store()

    var snapshot: Snapshot?
    var lastError: String?
    var refreshing = false
    private var refreshWithoutSchedulingRequested = false
    private var pendingEvidenceActions: Set<String> = []
    var lastRefresh: Date?
    var installing = false
    var updating = false
    var launchAtLogin: Bool = SMAppService.mainApp.status == .enabled
    private var pollTask: Task<Void, Never>?
    private var lastStatusKey = ""
    /// Probes requested from the panel whose worker has not yet registered itself in the ledger. Until the snapshot
    /// shows them running (or finished), the row keeps its "probing" state instead of flickering back to idle.
    private var pendingProbes: [String: Date] = [:]
    private var pendingFresh: Date?
    private let pendingTimeout: TimeInterval = 45
    private var lastHeartbeat = Date.distantPast
    private var lastTick = Date.distantPast

    var isAlert: Bool { (snapshot?.overall.downgraded ?? 0) > 0 }
    var isWarn: Bool { !isAlert && (snapshot?.overall.suspicious ?? 0) > 0 }
    var isUpgraded: Bool { !isAlert && !isWarn && (snapshot?.overall.upgraded ?? 0) > 0 }
    var isRunning: Bool { (snapshot?.overall.running ?? 0) > 0 }
    /// `NERFED_DEMO=1` renders synthetic English sample data (used for README screenshots).
    let demo = ProcessInfo.processInfo.environment["NERFED_DEMO"] == "1"

    func start(interval: Duration = .seconds(8)) {
        guard pollTask == nil else { return }
        appLog.info("store started, polling every \(interval.components.seconds, privacy: .public)s\(self.demo ? " (demo)" : "", privacy: .public)")
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: interval)
            }
        }
    }

    func refresh(allowScheduledWork: Bool = true) async {
        if refreshing {
            if !allowScheduledWork { refreshWithoutSchedulingRequested = true }
            return
        }
        refreshing = true
        defer {
            refreshing = false
            if refreshWithoutSchedulingRequested {
                refreshWithoutSchedulingRequested = false
                Task { [weak self] in await self?.refresh(allowScheduledWork: false) }
            }
        }
        do {
            let json = try await DGC.run(["snapshot", "--json"] + (demo ? ["--demo"] : []), timeout: 20)
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            var snap = try decoder.decode(Snapshot.self, from: Data(json.utf8))
            overlayPending(&snap)
            snapshot = snap
            lastError = nil
            lastRefresh = Date()
            let key = "\(snap.overall.status)|\(snap.overall.message)|\(snap.hooks?.state ?? "-")|\(snap.hooks?.desktopLoaded ?? false)"
            if key != lastStatusKey {
                lastStatusKey = key
                appLog.notice("status \(snap.overall.status, privacy: .public): \(snap.overall.message, privacy: .public) · hooks \(snap.hooks?.state ?? "?", privacy: .public) · desktop loaded \(snap.hooks?.desktopLoaded ?? false, privacy: .public)")
            }
            // Fresh-session heartbeat: the hooks fire it while threads are active; the app covers idle periods.
            if allowScheduledWork, snap.freshDue == true, snap.globalRunning != true, pendingFresh == nil, !demo,
               Date().timeIntervalSince(lastHeartbeat) > 300 {
                lastHeartbeat = Date()
                appLog.notice("fresh-session heartbeat due; starting a fresh probe")
                probeFresh()
            }
            // Thread heartbeat: the Stop hook runs the schedule while a thread is in use, so a thread that goes quiet
            // right after its due time would wait for a Stop event that may never come. The app covers that gap.
            if allowScheduledWork, !demo, Date().timeIntervalSince(lastTick) > 60,
               snap.threads.contains(where: { $0.due && $0.active && !$0.probeRunning && !$0.halted }) {
                lastTick = Date()
                tick()
            }
        } catch {
            if lastError != error.localizedDescription {
                appLog.error("snapshot failed: \(error.localizedDescription, privacy: .public)")
            }
            lastError = error.localizedDescription
        }
    }

    /// Keep panel-requested probes in their "probing" state until the ledger catches up (worker start-up takes a
    /// second or two, and a poll can land in between).
    private func overlayPending(_ snap: inout Snapshot) {
        let now = Date()
        for (id, since) in pendingProbes {
            guard let i = snap.threads.firstIndex(where: { $0.id == id }) else { pendingProbes[id] = nil; continue }
            let t = snap.threads[i]
            let finishedAfter = [t.lastProbe?.finished, t.lastFailure?.finished].compactMap { $0 }.compactMap(parseISO).contains { $0 > since }
            if snap.threads[i].probeRunning || finishedAfter || now.timeIntervalSince(since) > pendingTimeout {
                pendingProbes[id] = nil
            } else {
                snap.threads[i].probeRunning = true
                snap.overall.running += 1
            }
        }
        if let since = pendingFresh {
            let finishedAfter = [snap.globalProbe?.finished, snap.globalFailure?.finished].compactMap { $0 }.compactMap(parseISO).contains { $0 > since }
            if snap.globalRunning == true || finishedAfter || now.timeIntervalSince(since) > pendingTimeout {
                pendingFresh = nil
            } else {
                snap.globalRunning = true
                snap.overall.running += 1
            }
        }
    }

    private func parseISO(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    /// Start (or retry) a background probe of one thread. The worker records the result; the next refresh shows it.
    func probe(_ thread: ThreadInfo) {
        appLog.notice("probe requested from panel for thread \(thread.id, privacy: .public)")
        do {
            try DGC.spawnDetached(["worker", "--thread", thread.id])
            pendingProbes[thread.id] = Date()
            if var snap = snapshot { overlayPending(&snap); snapshot = snap }
        } catch {
            appLog.error("probe spawn failed: \(error.localizedDescription, privacy: .public)")
            lastError = error.localizedDescription
        }
        Task { try? await Task.sleep(for: .seconds(3)); await refresh() }
    }

    /// Scheduler tick: probes threads that are due and still active but quiet, which no hook event would pick up.
    func tick() {
        appLog.notice("a due thread is active but quiet; running the scheduler tick")
        Task {
            do {
                let out = try await DGC.run(["tick"], timeout: 20).trimmingCharacters(in: .whitespacesAndNewlines)
                if !out.isEmpty { appLog.notice("tick: \(out.suffix(300), privacy: .public)") }
            } catch {
                appLog.error("tick failed: \(error.localizedDescription, privacy: .public)")
            }
            try? await Task.sleep(for: .seconds(3))
            await refresh()
        }
    }

    /// Global probe: brand-new ephemeral sessions with the default model, no thread context.
    func probeFresh() {
        appLog.notice("fresh-session probe requested from panel")
        do {
            try DGC.spawnDetached(["worker", "--fresh"])
            pendingFresh = Date()
            if var snap = snapshot { overlayPending(&snap); snapshot = snap }
        } catch {
            appLog.error("fresh probe spawn failed: \(error.localizedDescription, privacy: .public)")
            lastError = error.localizedDescription
        }
        Task { try? await Task.sleep(for: .seconds(3)); await refresh() }
    }

    /// First run from a downloaded .app: register the bundled plugin with Codex and trust the hooks.
    func installPlugin() async {
        installing = true
        defer { installing = false }
        appLog.notice("installing the bundled plugin into Codex")
        do {
            let out = try await DGC.run(["setup", "--trust-hooks"], timeout: 240)
            appLog.notice("setup: \(out.suffix(400), privacy: .public)")
        } catch {
            // setup exits non-zero when doctor still lists a problem; the snapshot below shows what is left
            appLog.error("setup returned an error: \(error.localizedDescription, privacy: .public)")
        }
        await refresh()
        if snapshot?.install?.pluginEnabled != true {
            lastError = L10n.tr("Install did not complete; run ./install.sh from a checkout or see nerfed doctor")
        }
    }

    func trustHooks() async {
        appLog.notice("trusting hooks from the panel")
        do { _ = try await DGC.run(["hooks", "trust"], timeout: 90) } catch { lastError = error.localizedDescription }
        await refresh()
    }

    func resume(_ thread: ThreadInfo) async {
        appLog.notice("resume requested for thread \(thread.id, privacy: .public)")
        do { _ = try await DGC.run(["resume", "--thread", thread.id]) } catch { lastError = error.localizedDescription }
        await refresh()
    }

    func evidenceActionPending(threadID: String, evidenceID: String) -> Bool {
        pendingEvidenceActions.contains(threadID + ":" + evidenceID)
    }

    func setEvidenceExpected(threadID: String, evidence: EvidenceInfo) async {
        guard !demo, snapshot?.demo != true, let evidenceID = evidence.evidenceId, !evidenceID.isEmpty,
              evidence.kind == "context_window_change", evidence.severity == "hard" else { return }
        let key = threadID + ":" + evidenceID
        guard pendingEvidenceActions.insert(key).inserted else { return }
        defer { pendingEvidenceActions.remove(key) }
        let arguments = ["evidence-expected", "--thread", threadID, "--evidence", evidenceID]
            + (evidence.expected == true ? ["--undo"] : [])
        do {
            _ = try await DGC.run(arguments)
            // A manual confirmation only changes the ledger; it must not start a probe.
            await refresh(allowScheduledWork: false)
        } catch {
            lastError = error.localizedDescription
        }
    }

    func setConfig(_ key: String, _ value: String) async {
        appLog.notice("config \(key, privacy: .public) = \(value, privacy: .public)")
        do { _ = try await DGC.run(["config", "set", key, value]) } catch { lastError = error.localizedDescription }
        await refresh()
    }

    /// "Copy report" in a row: the thread's plain-text report onto the clipboard.
    func copy(_ text: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
        appLog.notice("report copied to the clipboard")
    }

    /// The footer's update link: `nerfed update-install` downloads the release, verifies it, stops this app, swaps
    /// the bundle and relaunches. It runs detached so it outlives the app it replaces.
    func installUpdate() {
        appLog.notice("update requested from the panel")
        updating = true
        do {
            try DGC.spawnDetached(["update-install", "--app", Bundle.main.bundlePath, "--pid", String(ProcessInfo.processInfo.processIdentifier)])
        } catch {
            updating = false
            lastError = error.localizedDescription
            return
        }
        Task {  // still running two minutes later means it failed; the link comes back with the reason
            try? await Task.sleep(for: .seconds(120))
            updating = false
            await refresh()
        }
    }

    func openURL(_ s: String) {
        if let u = URL(string: s) { NSWorkspace.shared.open(u) }
    }

    /// Open the thread's folder in Finder. (`selectFile(nil, inFileViewerRootedAtPath:)` only activated Finder and
    /// left whatever window was in front, so it looked like the wrong folder.)
    func reveal(_ path: String) {
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
            appLog.error("reveal: folder missing \(path, privacy: .public)")
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true))
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            launchAtLogin = enabled
            appLog.notice("launch at login \(enabled ? "enabled" : "disabled", privacy: .public)")
        } catch {
            lastError = error.localizedDescription
            launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }
}
