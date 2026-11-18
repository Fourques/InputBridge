// InputBridge: read an actual native drag at the configured outer screen edge.
// GPL-3.0-or-later. No general clipboard reads and no global keyboard shortcut.
import Cocoa

final class EdgeView: NSView {
    private var sequence = -1
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty,
            urls.count <= 64, urls.allSatisfy({ $0.isFileURL && ($0.host == nil || $0.host == "" || $0.host == "localhost") }) else { return [] }
        if sequence != sender.draggingSequenceNumber {
            sequence = sender.draggingSequenceNumber
            emit(["event": "edgeDrag", "drag": UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                  "paths": urls.map { $0.path }])
        }
        return .copy
    }
    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool { true }
}

final class EdgeCapture {
    static let shared = EdgeCapture()
    private var panels: [NSPanel] = []
    private var timer: Timer?
    func stop() {
        timer?.invalidate(); timer = nil
        panels.forEach { $0.orderOut(nil) }; panels.removeAll()
    }
    func configure(side: String) {
        stop()
        guard ["left", "right", "up", "down"].contains(side), !NSScreen.screens.isEmpty else { return }
        let outer = NSScreen.screens.reduce(NSRect.null) { $0.union($1.frame) }
        for screen in NSScreen.screens {
            let f = screen.frame
            let touches = side == "left" ? f.minX == outer.minX : side == "right" ? f.maxX == outer.maxX : side == "up" ? f.maxY == outer.maxY : f.minY == outer.minY
            if !touches { continue }
            let rect: NSRect
            switch side {
            case "left": rect = NSRect(x: f.minX, y: f.minY, width: 3, height: f.height)
            case "right": rect = NSRect(x: f.maxX-3, y: f.minY, width: 3, height: f.height)
            case "up": rect = NSRect(x: f.minX, y: f.maxY-3, width: f.width, height: 3)
            default: rect = NSRect(x: f.minX, y: f.minY, width: f.width, height: 3)
            }
            let panel = NSPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.isOpaque = false; panel.backgroundColor = NSColor(white: 1, alpha: 0.01)
            panel.hasShadow = false; panel.level = .popUpMenu; panel.isReleasedWhenClosed = false
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            let view = EdgeView(frame: NSRect(origin: .zero, size: rect.size))
            view.registerForDraggedTypes([.fileURL, .init("NSFilenamesPboardType")])
            panel.contentView = view; panels.append(panel)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0/60, repeats: true) { [weak self] _ in
            let held = NSEvent.pressedMouseButtons & 1 != 0
            self?.panels.forEach { held ? $0.orderFrontRegardless() : $0.orderOut(nil) }
        }
    }
}
