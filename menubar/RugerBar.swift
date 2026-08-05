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

/// The launchd job that imports and syncs. Kickstarted from "Run a tick".
let TICK_LABEL = "ai.ruger.wispr"

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

/// Catches Command-Return before the text view turns it into a newline, and backs
/// up the Edit menu for the standard editing shortcuts.
final class CaptureTextView: NSTextView {
    var onSubmit: (() -> Void)?

    /// NSTextView has no placeholder, so it is drawn. An empty box with no prompt
    /// is the difference between "type here" and "is this thing on".
    var placeholder: String = "" { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let inset = textContainerInset
        let origin = NSPoint(x: inset.width + (textContainer?.lineFragmentPadding ?? 0),
                             y: inset.height)
        (placeholder as NSString).draw(
            at: origin,
            withAttributes: [.font: font ?? .systemFont(ofSize: 13),
                             .foregroundColor: NSColor.tertiaryLabelColor])
    }

    override func didChangeText() {
        super.didChangeText()
        needsDisplay = true          // the placeholder appears again when emptied
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard event.modifierFlags.contains(.command) else {
            return super.performKeyEquivalent(with: event)
        }

        if event.keyCode == 36 {                       // Return
            onSubmit?()
            return true
        }

        // AppKit reaches here only when the main menu did not claim the shortcut.
        // The Edit menu normally does, so this is a safety net for the case that
        // put it here in the first place: no menu, or the app not yet active, and
        // ⌘V silently doing nothing in a box you are staring at.
        switch event.charactersIgnoringModifiers?.lowercased() {
        case "v": paste(nil); return true
        case "c": copy(nil); return true
        case "x": cut(nil); return true
        case "a": selectAll(nil); return true
        case "z":
            let manager = undoManager
            if event.modifierFlags.contains(.shift) { manager?.redo() } else { manager?.undo() }
            return true
        default:
            return super.performKeyEquivalent(with: event)
        }
    }
}

/// One place for the numbers the panel is built from, so nothing is hand-placed.
private enum Metric {
    static let width: CGFloat = 380
    static let pad: CGFloat = 16
    static let gap: CGFloat = 10
    static let boxHeight: CGFloat = 104
}

final class CaptureViewController: NSViewController {
    private let textView = CaptureTextView()
    private let statusDot = NSTextField(labelWithString: "\u{25CF}")
    private let statusLine = NSTextField(labelWithString: "")
    private let countLine = NSTextField(labelWithString: "")
    private let overdueTag = NSTextField(labelWithString: "")
    private let boardButton = NSButton()
    private var snapshot = Snapshot()

    var onCaptured: (() -> Void)?

    /// A quiet text button: the panel has one loud control and everything else
    /// stays out of the way.
    private func quietButton(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .inline
        b.isBordered = false
        b.font = .systemFont(ofSize: 11.5)
        b.contentTintColor = .secondaryLabelColor
        return b
    }

    private func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular,
                       colour: NSColor = .labelColor) -> NSTextField {
        let f = NSTextField(labelWithString: text)
        f.font = .systemFont(ofSize: size, weight: weight)
        f.textColor = colour
        return f
    }

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: Metric.width, height: 260))

        // --- the box you type into ---------------------------------------
        textView.font = .systemFont(ofSize: 13)
        textView.isRichText = false
        // Dictation and pasted prose bring smart quotes and dashes. The quote check
        // tolerates them, but off keeps what is stored closer to what was said.
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.textContainerInset = NSSize(width: 8, height: 9)
        textView.allowsUndo = true          // or the Edit menu's Undo does nothing
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.drawsBackground = false
        textView.placeholder = "Type, or dictate. Several tasks in one go is fine."
        textView.onSubmit = { [weak self] in self?.capture() }

        let scroll = NSScrollView()
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false

        // A rounded, bordered surface rather than AppKit's bezel, which reads as
        // a 2005 utility panel next to the rest of the product. NSBox resolves
        // semantic colours per appearance, so this follows light and dark.
        let box = NSBox()
        box.boxType = .custom
        box.fillColor = .textBackgroundColor
        box.borderColor = .separatorColor
        box.borderWidth = 1
        box.cornerRadius = 7
        box.contentViewMargins = .zero
        box.translatesAutoresizingMaskIntoConstraints = false
        box.contentView = scroll

        // --- the one loud control ----------------------------------------
        let capture = NSButton(title: "Capture", target: self,
                               action: #selector(captureClicked))
        capture.bezelStyle = .rounded
        capture.keyEquivalent = "\r"
        capture.keyEquivalentModifierMask = [.command]
        capture.controlSize = .regular
        // The panel's one loud control. It cannot be the window's default button
        // (that would bind plain Return, which the box needs for newlines), so
        // the accent is applied directly.
        capture.bezelColor = .controlAccentColor

        let hint = label("\u{2318}\u{21A9} to capture", size: 11.5,
                         colour: .tertiaryLabelColor)
        let hintRow = NSStackView(views: [hint, NSView(), capture])
        hintRow.orientation = .horizontal
        hintRow.alignment = .centerY

        // --- what is going on ---------------------------------------------
        statusDot.font = .systemFont(ofSize: 9)
        statusLine.font = .systemFont(ofSize: 11.5)
        statusLine.textColor = .secondaryLabelColor
        countLine.font = .systemFont(ofSize: 11.5)
        countLine.textColor = .tertiaryLabelColor

        overdueTag.font = .systemFont(ofSize: 11, weight: .medium)
        overdueTag.textColor = .systemRed

        let statusRow = NSStackView(views: [statusDot, statusLine, NSView(), overdueTag])
        statusRow.orientation = .horizontal
        statusRow.alignment = .centerY
        statusRow.spacing = 6

        boardButton.title = "Open board"
        boardButton.bezelStyle = .inline
        boardButton.isBordered = false
        boardButton.font = .systemFont(ofSize: 11.5)
        boardButton.contentTintColor = .secondaryLabelColor
        boardButton.target = self
        boardButton.action = #selector(openBoard)

        let actions = NSStackView(views: [
            boardButton,
            quietButton("Run a tick", #selector(runTick)),
            NSView(),
            quietButton("Quit", #selector(quit)),
        ])
        actions.orientation = .horizontal
        actions.alignment = .centerY
        actions.spacing = 12

        let divider = NSBox()
        divider.boxType = .separator

        // --- one column, one spacing scale --------------------------------
        let column = NSStackView(views: [
            label("What needs doing?", size: 13, weight: .semibold),
            box, hintRow, divider, statusRow, countLine, actions,
        ])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = Metric.gap
        column.translatesAutoresizingMaskIntoConstraints = false
        column.setCustomSpacing(6, after: box)
        column.setCustomSpacing(14, after: hintRow)
        column.setCustomSpacing(12, after: divider)
        column.setCustomSpacing(6, after: statusRow)
        column.setCustomSpacing(14, after: countLine)
        root.addSubview(column)

        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: root.leadingAnchor,
                                            constant: Metric.pad),
            column.trailingAnchor.constraint(equalTo: root.trailingAnchor,
                                             constant: -Metric.pad),
            column.topAnchor.constraint(equalTo: root.topAnchor, constant: Metric.pad),
            column.bottomAnchor.constraint(equalTo: root.bottomAnchor,
                                           constant: -Metric.pad),
            box.heightAnchor.constraint(equalToConstant: Metric.boxHeight),
            box.widthAnchor.constraint(equalTo: column.widthAnchor),
            divider.widthAnchor.constraint(equalTo: column.widthAnchor),
            hintRow.widthAnchor.constraint(equalTo: column.widthAnchor),
            statusRow.widthAnchor.constraint(equalTo: column.widthAnchor),
            actions.widthAnchor.constraint(equalTo: column.widthAnchor),
            scroll.topAnchor.constraint(equalTo: box.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: box.bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: box.trailingAnchor),
        ])

        view = root
    }

    func apply(_ snap: Snapshot) {
        snapshot = snap
        let fresh = !snap.stale
        statusDot.textColor = fresh ? .systemGreen : .systemRed
        statusLine.stringValue = fresh
            ? "Last import \(snap.ago)"
            : "Timer looks stopped \u{00B7} last ran \(snap.ago)"
        statusLine.textColor = fresh ? .secondaryLabelColor : .systemRed
        countLine.stringValue =
            "\(snap.total) in Notion \u{00B7} \(snap.todo) to do \u{00B7} \(snap.done) done"
        overdueTag.stringValue = snap.overdue > 0 ? "\(snap.overdue) overdue" : ""
        // A dead link is worse than no link — the same rule the log follows.
        boardButton.isHidden = !snap.serving
    }

    func focusBox() {
        view.window?.makeFirstResponder(textView)
    }

    func clear() {
        textView.string = ""
        textView.needsDisplay = true
    }

    @objc private func captureClicked() { capture() }
    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func runTick() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["kickstart", "-p", "gui/\(getuid())/\(TICK_LABEL)"]
        try? task.run()
    }

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

// MARK: - The main menu

/// Cut, Copy, Paste, Select All and Undo are dispatched by the application's Edit
/// menu — AppKit routes ⌘V by looking for a matching key equivalent there, then
/// sending `paste:` to the first responder. A plain executable has no main menu
/// unless it builds one, so without this the text box silently refuses ⌘V while
/// typing and dictation work fine.
///
/// Undo and Redo stay as raw selectors because no imported protocol declares
/// them; the rest resolve through AppKit, so they get the checked `#selector`
/// form and the build stays warning-free.
func buildMainMenu() -> NSMenu {
    let main = NSMenu()

    let appItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "Quit Ruger",
                    action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    main.addItem(appItem)

    let editItem = NSMenuItem()
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    if let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")),
                               keyEquivalent: "z") as NSMenuItem? {
        redo.keyEquivalentModifierMask = [.command, .shift]
    }
    edit.addItem(.separator())
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    if let plain = edit.addItem(withTitle: "Paste and Match Style",
                                action: #selector(NSTextView.pasteAsPlainText(_:)),
                                keyEquivalent: "v") as NSMenuItem? {
        plain.keyEquivalentModifierMask = [.command, .option, .shift]
    }
    edit.addItem(withTitle: "Select All",
                 action: #selector(NSStandardKeyBindingResponding.selectAll(_:)),
                 keyEquivalent: "a")
    editItem.submenu = edit
    main.addItem(editItem)

    return main
}

// MARK: - The status item

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private let controller = CaptureViewController()
    private var timer: Timer?
    private var keyMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = buildMainMenu()

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
        // Menu key equivalents only reach an app that is active, so ⌘V depends on
        // this as much as on the Edit menu existing.
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        // Focus after showing: before the popover has a window there is no first
        // responder to set, and the box would open needing a click.
        controller.focusBox()
    }
}

/// `RugerBar --snapshot out.png [--light] [--stale]` renders the popover to a
/// file and exits. A popover cannot be screenshotted headlessly and a menu bar
/// is not a thing you can measure by eye, so this is how the layout gets
/// checked — the same reason `board.html` takes `?view=` and `?theme=`.
func renderSnapshot(to path: String, light: Bool, stale: Bool) {
    let controller = CaptureViewController()
    let view = controller.view
    view.appearance = NSAppearance(named: light ? .aqua : .darkAqua)

    var snap = Snapshot()
    snap.total = 14; snap.todo = 9; snap.doing = 1; snap.done = 4
    snap.overdue = 2; snap.notes = 9; snap.pushed = 14
    snap.tickAge = stale ? 7200 : 140
    snap.stale = stale
    snap.serving = true
    controller.apply(snap)

    // A popover supplies its own material, so the panel itself is transparent.
    // Rendered to a file that means white paper, and every label drawn in
    // `.labelColor` came out white-on-white and looked missing. The backdrop is
    // part of the harness, not of the panel.
    let backdrop = NSBox(frame: view.bounds)
    backdrop.boxType = .custom
    backdrop.borderWidth = 0
    backdrop.fillColor = .windowBackgroundColor
    backdrop.autoresizingMask = [.width, .height]
    view.addSubview(backdrop, positioned: .below, relativeTo: nil)

    // Hosted in a real (offscreen) window: text does not draw through
    // `cacheDisplay` on a view that has never had one.
    let window = NSWindow(contentRect: view.bounds, styleMask: [.borderless],
                          backing: .buffered, defer: false)
    window.appearance = view.appearance
    window.contentView = view
    window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
    window.orderFront(nil)
    view.layoutSubtreeIfNeeded()
    window.displayIfNeeded()

    // Through the PDF path rather than `cacheDisplay`: the latter renders the
    // geometry but drops the text of layer-backed labels, which reads as a broken
    // layout when the layout is fine.
    let pdf = view.dataWithPDF(inside: view.bounds)
    guard let image = NSImage(data: pdf),
          let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff) else { return }

    if let data = rep.representation(using: .png, properties: [:]) {
        try? data.write(to: URL(fileURLWithPath: path))
        print("wrote \(path)  \(Int(view.bounds.width))x\(Int(view.bounds.height))")
    }
}

let args = CommandLine.arguments
if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    renderSnapshot(to: args[i + 1],
                   light: args.contains("--light"),
                   stale: args.contains("--stale"))
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// .accessory: a menu bar app with no Dock icon and no main window.
app.setActivationPolicy(.accessory)
app.run()
