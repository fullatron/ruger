// Ruger's menu bar app: click the icon and the capture box is already there.
//
//     sh scripts/build-menubar.sh        # -> build/RugerBar
//
// Why this exists at all, given SwiftBar did the job: SwiftBar plugins are text
// output plus click actions, with no input widget, so the box could only ever be
// a second click away in a separate window. A popover is one click and no window.
//
// It owns no logic. Counts come from `pkm status --json` and a capture is handed
// to `pkm capture --notify`, so the rules stay in Python where they are testable
// and this file cannot disagree with the board.

import AppKit
import Foundation

// MARK: - Where the repo is

/// Derived from this binary's own location (build/RugerBar -> repo), so a clone
/// works anywhere. `RUGER_REPO` overrides it for an unusual install.
let repoURL: URL = {
    if let override = ProcessInfo.processInfo.environment["RUGER_REPO"], !override.isEmpty {
        return URL(fileURLWithPath: override).standardizedFileURL
    }
    let binary = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
    return binary.deletingLastPathComponent().deletingLastPathComponent().standardizedFileURL
}()

let pythonURL = repoURL.appendingPathComponent(".venv/bin/python")

/// Run a `pkm` subcommand off the main thread. `input` is piped to stdin, which is
/// how captured text travels: an argument would have to survive a shell.
func runPKM(_ arguments: [String], input: String? = nil,
            then finish: ((String) -> Void)? = nil) {
    DispatchQueue.global(qos: .userInitiated).async {
        let task = Process()
        task.executableURL = pythonURL
        task.arguments = ["-m", "pkm"] + arguments
        task.currentDirectoryURL = repoURL

        let out = Pipe()
        task.standardOutput = out
        task.standardError = Pipe()

        let stdin = Pipe()
        if input != nil { task.standardInput = stdin }

        do {
            try task.run()
        } catch {
            DispatchQueue.main.async { finish?("") }
            return
        }

        if let input {
            stdin.fileHandleForWriting.write(Data(input.utf8))
            stdin.fileHandleForWriting.closeFile()
        }

        let data = out.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        DispatchQueue.main.async { finish?(text) }
    }
}

// MARK: - What the icon and the footer show

struct Snapshot {
    var total = 0, todo = 0, doing = 0, done = 0, overdue = 0, notes = 0, pushed = 0
    var tickAge: Int? = nil
    var stale = true
    var serving = false
    var url = "http://127.0.0.1:8765"

    /// `pkm status --json` is the single source for all of this.
    static func parse(_ json: String) -> Snapshot? {
        guard let data = json.data(using: .utf8),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let board = root["board"] as? [String: Any],
              let tick = root["tick"] as? [String: Any]
        else { return nil }

        var snap = Snapshot()
        func count(_ key: String) -> Int { (board[key] as? Int) ?? 0 }
        snap.total = count("total"); snap.todo = count("todo")
        snap.doing = count("doing"); snap.done = count("done")
        snap.overdue = count("overdue"); snap.notes = count("notes")
        snap.pushed = count("pushed")
        snap.tickAge = tick["age"] as? Int
        snap.stale = (tick["stale"] as? Bool) ?? true
        snap.serving = (root["serving"] as? Bool) ?? false
        snap.url = (root["url"] as? String) ?? snap.url
        return snap
    }

    /// Same wording as `pkm status`, deliberately: two surfaces reading one number
    /// should not describe it differently.
    var ago: String {
        guard let age = tickAge else { return "never" }
        if age < 90 { return "just now" }
        let minutes = age / 60
        if minutes < 60 { return "\(minutes) min ago" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) hour\(hours == 1 ? "" : "s") ago" }
        let days = hours / 24
        return "\(days) day\(days == 1 ? "" : "s") ago"
    }

    var footer: String {
        var line = "\(total) commitment\(total == 1 ? "" : "s") · \(todo) to do · \(doing) doing"
        if overdue > 0 { line += " · \(overdue) overdue" }
        line += "\n\(notes) note\(notes == 1 ? "" : "s") · \(pushed) pushed"
        line += stale ? " · timer looks stopped" : " · tick \(ago)"
        return line
    }
}

// MARK: - The box

/// Catches Command-Return before the text view turns it into a newline.
final class CaptureTextView: NSTextView {
    var onSubmit: (() -> Void)?

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command), event.keyCode == 36 {
            onSubmit?()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

final class CaptureViewController: NSViewController {
    private let width: CGFloat = 360
    private let textView = CaptureTextView()
    private let footer = NSTextField(labelWithString: "")
    private let boardButton = NSButton()
    private var snapshot = Snapshot()

    var onCaptured: (() -> Void)?

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 268))

        let title = NSTextField(labelWithString: "What needs doing?")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.frame = NSRect(x: 14, y: 238, width: width - 28, height: 18)
        root.addSubview(title)

        textView.frame = NSRect(x: 0, y: 0, width: width - 28, height: 120)
        textView.font = .systemFont(ofSize: 13)
        textView.isRichText = false
        // Dictation and pasted prose bring smart quotes and dashes. The quote check
        // tolerates them, but off keeps what is stored closer to what was said.
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.textContainerInset = NSSize(width: 5, height: 7)
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.onSubmit = { [weak self] in self?.capture() }

        let scroll = NSScrollView(frame: NSRect(x: 14, y: 108, width: width - 28, height: 124))
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = true
        root.addSubview(scroll)

        let hint = NSTextField(labelWithString: "⌘↩ to capture · Esc to close")
        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor
        hint.frame = NSRect(x: 14, y: 86, width: width - 28, height: 14)
        root.addSubview(hint)

        let capture = NSButton(title: "Capture", target: self, action: #selector(captureClicked))
        capture.bezelStyle = .rounded
        capture.controlSize = .small
        capture.frame = NSRect(x: width - 94, y: 56, width: 80, height: 24)
        root.addSubview(capture)

        let line = NSBox(frame: NSRect(x: 0, y: 46, width: width, height: 1))
        line.boxType = .separator
        root.addSubview(line)

        footer.font = .systemFont(ofSize: 11)
        footer.textColor = .secondaryLabelColor
        footer.maximumNumberOfLines = 2
        footer.frame = NSRect(x: 14, y: 14, width: width - 120, height: 28)
        root.addSubview(footer)

        boardButton.title = "Open board"
        boardButton.bezelStyle = .inline
        boardButton.isBordered = false
        boardButton.font = .systemFont(ofSize: 11)
        boardButton.contentTintColor = .linkColor
        boardButton.target = self
        boardButton.action = #selector(openBoard)
        boardButton.frame = NSRect(x: width - 100, y: 24, width: 86, height: 16)
        root.addSubview(boardButton)

        let quit = NSButton(title: "Quit", target: NSApp, action: #selector(NSApplication.terminate(_:)))
        quit.bezelStyle = .inline
        quit.isBordered = false
        quit.font = .systemFont(ofSize: 11)
        quit.contentTintColor = .secondaryLabelColor
        quit.frame = NSRect(x: width - 100, y: 6, width: 86, height: 16)
        root.addSubview(quit)

        view = root
    }

    func apply(_ snap: Snapshot) {
        snapshot = snap
        footer.stringValue = snap.footer
        // A dead link is worse than no link — the same rule the SwiftBar menu follows.
        boardButton.isHidden = !snap.serving
    }

    func focusBox() {
        view.window?.makeFirstResponder(textView)
    }

    func clear() {
        textView.string = ""
    }

    @objc private func captureClicked() { capture() }

    @objc private func openBoard() {
        if let url = URL(string: snapshot.url) { NSWorkspace.shared.open(url) }
    }

    private func capture() {
        let text = textView.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        // Cleared and dismissed straight away: `pkm capture --notify` reports when
        // it lands, and holding the box open for three seconds would undo the point.
        clear()
        runPKM(["capture", "--notify"], input: text)
        onCaptured?()
    }
}

// MARK: - The status item

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private let controller = CaptureViewController()
    private var timer: Timer?
    private var keyMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggle)
        render(Snapshot())

        popover.contentViewController = controller
        popover.behavior = .transient
        popover.animates = false
        controller.onCaptured = { [weak self] in
            self?.popover.performClose(nil)
            // Give the pipeline a moment to land before asking for new counts.
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) { self?.refresh() }
        }

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.refresh()
        }

        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.popover.isShown else { return event }
            if event.keyCode == 53 {                    // Escape
                self.popover.performClose(nil)
                return nil
            }
            return event
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        timer?.invalidate()
    }

    private func render(_ snap: Snapshot) {
        guard let button = statusItem.button else { return }
        let fresh = !snap.stale
        let dot = fresh ? "◉" : "◌"
        let colour: NSColor = fresh
            ? NSColor(calibratedRed: 0.30, green: 0.67, blue: 0.60, alpha: 1)   // #4dab9a
            : NSColor(calibratedRed: 0.92, green: 0.34, blue: 0.34, alpha: 1)   // #eb5757
        button.attributedTitle = NSAttributedString(
            string: "\(dot) \(snap.total)",
            attributes: [.foregroundColor: colour,
                         .font: NSFont.systemFont(ofSize: 13)])
        button.toolTip = snap.footer
    }

    private func refresh() {
        runPKM(["status", "--json"]) { [weak self] json in
            guard let self, let snap = Snapshot.parse(json) else { return }
            self.render(snap)
            self.controller.apply(snap)
        }
    }

    @objc private func toggle() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        guard let button = statusItem.button else { return }
        refresh()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        // Focus after showing: before the popover has a window there is no first
        // responder to set, and the box would open needing a click.
        controller.focusBox()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// .accessory: a menu bar app with no Dock icon and no main window.
app.setActivationPolicy(.accessory)
app.run()
