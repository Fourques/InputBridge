// TwinDesk — Copyright (C) 2026 inukaasith. GPL-3.0, see LICENSE.
// Modified by InputBridge contributors for scoped drag cleanup and file previews.
// Files dragged in from the PC become a real drag on this Mac ("Drag files across
// the screen edge"). `dragStart` puts a nearly invisible panel under the pointer and
// injects a (marked) left-button-down into it; its mouseDown starts an NSDraggingSession with one
// NSFilePromiseProvider per dragged item. From then on the session follows the PC's injected
// pointer (k.mm arrive as leftMouseDragged), the PC's k.mu {b:0} drops. Main writes the promised
// files: event `dragWrite {drag, index, path}` → cmd `dragWritten {drag, index, error?}`.
// `dragCancel` ends the session over a panel that refuses everything (Esc would go to the
// frontmost app instead). Events: dragStarted {drag}, dragEnded {drag, result: 'dropped'|'none'|'failed'}.
// `dragLocal {drag, paths}`: the same with this Mac's own files (a drag of ours that went to the PC
// comes back home with the button still held), as file URLs: Finder moves or copies them as usual.
import Cocoa
import ImageIO
import UniformTypeIdentifiers

final class DragView: NSView {
  override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
  override func mouseDown(with event: NSEvent) { DragIn.shared.mouseDown(event, in: self) }
  // As a drop target (only while cancelling): refuse, so the session ends with no operation.
  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { [] }
  override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { [] }
  override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { false }
}

/// Takes our own file drag at the crossing edge: accepting it ends Finder's drag quietly (no
/// slide-back, no Esc to the frontmost app) while the files carry on to the other computer.
final class CatchView: NSView {
  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }
  override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }
  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool { true }
}

final class DragIn: NSObject, NSDraggingSource, NSFilePromiseProviderDelegate {
  static let shared = DragIn()
  private enum Phase { case idle, arming, dragging, cancelling }
  private var phase = Phase.idle
  private var drag = ""
  private var items: [(name: String, dir: Bool)] = []
  private var local: [URL] = []           // dragLocal: our own files instead of promises
  private var panel: NSPanel?
  private var catcher: NSPanel?
  private var session: NSDraggingSession?
  private var preview: NSImage?
  private var pending: (drag: String, start: () -> Void)?
  private var swallowing: String?
  private var writes: [String: (Error?) -> Void] = [:]   // "drag#index" -> completion (main queue)
  private let queue: OperationQueue = {
    let q = OperationQueue()
    q.qualityOfService = .userInitiated
    return q
  }()

  var active: Bool { phase != .idle }
  var capturingInitialPress: Bool { phase == .arming || phase == .cancelling || swallowing != nil }

  private func deferWhileCancelling(drag: String, start: @escaping () -> Void) -> Bool {
    guard phase == .cancelling || swallowing != nil else { return false }
    if pending == nil {
      pending = (drag, start)
    } else {
      emit(["event": "dragEnded", "drag": drag, "result": "failed", "reason": "busy"])
    }
    return true
  }

  func start(drag: String, items: [[String: Any]], preview: String?) {
    if deferWhileCancelling(drag: drag, start: { [weak self] in self?.start(drag: drag, items: items, preview: preview) }) { return }
    guard phase == .idle, pending == nil else { return emit(["event": "dragEnded", "drag": drag, "result": "failed", "reason": "busy"]) }
    local = []
    self.preview = DragIn.thumbnail(preview)
    arm(drag: drag, items: items.map { (($0["name"] as? String) ?? "file", ($0["dir"] as? Bool) ?? false) })
  }

  func startLocal(drag: String, paths: [String], preview: String?) {
    if deferWhileCancelling(drag: drag, start: { [weak self] in self?.startLocal(drag: drag, paths: paths, preview: preview) }) { return }
    guard phase == .idle, pending == nil else { return emit(["event": "dragEnded", "drag": drag, "result": "failed", "reason": "busy"]) }
    let urls = paths.map { URL(fileURLWithPath: $0) }.filter { FileManager.default.fileExists(atPath: $0.path) }
    guard !urls.isEmpty else { return emit(["event": "dragEnded", "drag": drag, "result": "failed"]) }
    local = urls
    self.preview = DragIn.thumbnail(preview)
    arm(drag: drag, items: urls.map { u in (u.lastPathComponent, (try? u.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true) })
  }

  private func arm(drag: String, items: [(name: String, dir: Bool)]) {
    if NSApp.activationPolicy() == .prohibited { NSApp.setActivationPolicy(.accessory) } // may own windows
    self.drag = drag
    self.items = items
    phase = .arming
    showPanel(size: 240)
    // The click into the panel; once more if the window server wasn't ready for it.
    for delay in [0.012, 0.055, 0.15, 0.45] {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [self] in
        guard phase == .arming, self.drag == drag else { return }
        let p = here()
        releaseButton()
        panel?.setFrameOrigin(cocoa(p, size: 240))
        injector.button(0, down: true, at: p)
      }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in
      guard phase == .arming, self.drag == drag else { return }
      log("drag: the panel never got the mouse-down")
      releaseButton()
      finish("failed", reason: "arming-timeout")
    }
  }

  func mouseDown(_ event: NSEvent, in view: NSView) {
    guard phase == .arming else { return }
    let at = view.convert(event.locationInWindow, from: nil)
    let dragging: [NSDraggingItem] = items.enumerated().map { i, it in
      let type: UTType = it.dir ? .folder : (UTType(filenameExtension: (it.name as NSString).pathExtension) ?? .data)
      let d: NSDraggingItem
      if i < local.count {
        d = NSDraggingItem(pasteboardWriter: local[i] as NSURL)
      } else {
        let provider = NSFilePromiseProvider(fileType: type.identifier, delegate: self)
        provider.userInfo = ["drag": drag, "index": i, "name": it.name]
        d = NSDraggingItem(pasteboardWriter: provider)
      }
      let img = DragIn.image(name: it.name, type: type, preview: i == 0 ? preview : nil)
      let o = CGFloat(min(i, 4)) * 3
      d.setDraggingFrame(NSRect(x: at.x - img.size.width / 2 + o, y: at.y - 32 - o, width: img.size.width, height: img.size.height), contents: img)
      return d
    }
    let session = view.beginDraggingSession(with: dragging, event: event, source: self)
    self.session = session
    session.animatesToStartingPositionsOnCancelOrFail = false
    session.draggingFormation = .stack
    phase = .dragging
    emit(["event": "dragStarted", "drag": drag])
    DispatchQueue.main.async { [self] in
      if phase == .dragging { hidePanel() }
    }
  }

  func buttonUp(drag: String) {
    if pending?.drag == drag { return cancel(drag: drag) }
    guard drag == self.drag else { return }
    switch phase {
    case .arming: cancel(drag: drag)
    case .dragging: releaseButton()
    case .idle, .cancelling: break
    }
  }

  /// End the drag without a drop (owner went home, Esc, drag.cancel, releaseAll).
  func cancel(drag: String? = nil) {
    if let waiting = pending, drag == nil || drag == waiting.drag {
      pending = nil
      emit(["event": "dragEnded", "drag": waiting.drag, "result": "none"])
      if drag != nil { return }
    }
    if let drag, drag != self.drag { return }
    switch phase {
    case .idle, .cancelling:
      return
    case .arming:
      releaseButton()
      finish("none")
    case .dragging:
      phase = .cancelling
      injector.heldButtons.remove(0) // released here, not by a later releaseAll
      showPanel(size: 160)
      let d = self.drag
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.012) { [self] in
        guard phase == .cancelling, self.drag == d else { return }
        let p = here()
        injector.mouse(.leftMouseDragged, at: p, button: 0, clickState: 1)
        injector.mouse(.leftMouseUp, at: p, button: 0, clickState: 1)
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [self] in
        if phase == .cancelling, self.drag == d { finish("none") }
      }
    }
  }

  /// Main wrote (or failed to write) the promised item.
  func written(drag: String, index: Int, error: String?) {
    guard let done = writes.removeValue(forKey: "\(drag)#\(index)") else { return }
    done(error.map { NSError(domain: "TwinDesk", code: 1, userInfo: [NSLocalizedDescriptionKey: $0]) })
  }

  private func finish(_ result: String, reason: String? = nil, moveTarget: Bool = false) {
    let d = drag
    phase = .idle
    session = nil
    preview = nil
    injector.heldButtons.remove(0)
    hidePanel()
    if result != "dropped" {
      for key in writes.keys.filter({ $0.hasPrefix("\(d)#") }) {
        writes.removeValue(forKey: key)?(CocoaError(.userCancelled))
      }
    }
    var event: [String: Any] = ["event": "dragEnded", "drag": d, "result": result, "moveTarget": moveTarget]
    if let reason { event["reason"] = reason }
    emit(event)
    startPendingIfReady()
  }

  private func startPendingIfReady() {
    DispatchQueue.main.async { [weak self] in
      guard let self, phase == .idle, swallowing == nil, let waiting = pending else { return }
      pending = nil
      waiting.start()
    }
  }

  private func releaseButton() {
    if injector.heldButtons.contains(0) { injector.button(0, down: false, at: here()) }
  }

  /// End this Mac's own file drag at `p` (it crossed the edge): drop it on a panel that accepts it.
  func swallowLocalDrag(drag: String, at p: CGPoint) {
    if NSApp.activationPolicy() == .prohibited { NSApp.setActivationPolicy(.accessory) }
    let c = catcher ?? {
      let w = DragIn.makePanel(CatchView())
      catcher = w
      return w
    }()
    c.setFrame(NSRect(origin: cocoa(p, size: 120), size: NSSize(width: 120, height: 120)), display: false)
    c.orderFrontRegardless()
    swallowing = drag
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.012) { [self] in
      guard swallowing == drag else { return }
      injector.mouse(.leftMouseDragged, at: p, button: 0, clickState: 1)
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.012) { [self] in
        guard swallowing == drag else { return }
        injector.mouse(.leftMouseUp, at: p, button: 0, clickState: 1)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.018) { [self] in
          guard swallowing == drag else { return }
          c.orderOut(nil)
          swallowing = nil
          startPendingIfReady()
        }
      }
    }
  }

  private static func makePanel(_ v: NSView) -> NSPanel {
    let w = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    w.isOpaque = false
    w.backgroundColor = NSColor(white: 1, alpha: 0.01) // fully clear windows let clicks through
    w.hasShadow = false
    w.level = .popUpMenu
    w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    w.isReleasedWhenClosed = false
    w.ignoresMouseEvents = false
    v.registerForDraggedTypes(NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) } + [.fileURL])
    w.contentView = v
    return w
  }

  // ---- the panel under the pointer ----

  private func here() -> CGPoint { CGEvent(source: nil)?.location ?? .zero }

  /// Cocoa origin of a size×size panel centred on a CG (top-left) point.
  private func cocoa(_ p: CGPoint, size: CGFloat) -> NSPoint {
    let h = NSScreen.screens.first?.frame.height ?? 0
    return NSPoint(x: p.x - size / 2, y: h - p.y - size / 2)
  }

  private func showPanel(size: CGFloat) {
    let p = panel ?? {
      let w = DragIn.makePanel(DragView())
      panel = w
      return w
    }()
    p.ignoresMouseEvents = false
    p.setFrame(NSRect(origin: cocoa(here(), size: size), size: NSSize(width: size, height: size)), display: false)
    p.orderFrontRegardless()
  }

  private func hidePanel() {
    panel?.orderOut(nil)
  }

  // ---- NSDraggingSource ----

  func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
    if context == .withinApplication {
      // A restored local drag must be able to reach our own screen-edge view.
      // Incoming promises still cannot be dropped back into the helper.
      return local.isEmpty ? [] : .copy
    }
    return local.isEmpty ? .copy : [.copy, .move, .link, .generic]
  }

  func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) {
    guard self.session === session, phase == .dragging || phase == .cancelling else { return }
    let accepted = !operation.isEmpty && phase == .dragging
    finish(accepted ? "dropped" : "none", moveTarget: accepted && local.isEmpty && finderAtDrop())
  }

  private func finderAtDrop() -> Bool {
    guard let point = CGEvent(source: nil)?.location,
          let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return false }
    for window in windows {
      guard let pid = window[kCGWindowOwnerPID as String] as? pid_t, pid != getpid(),
            let layer = window[kCGWindowLayer as String] as? Int, layer >= 0,
            let bounds = window[kCGWindowBounds as String] as? NSDictionary,
            let rect = CGRect(dictionaryRepresentation: bounds), rect.contains(point),
            let opacity = window[kCGWindowAlpha as String] as? Double, opacity > 0.05
      else { continue }
      return NSRunningApplication(processIdentifier: pid)?.bundleIdentifier == "com.apple.finder"
    }
    // The desktop has no ordinary application window at this point.
    return NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.apple.finder"
  }

  // ---- NSFilePromiseProviderDelegate ----

  private func info(_ p: NSFilePromiseProvider) -> (String, Int)? {
    guard let u = p.userInfo as? [String: Any], let d = u["drag"] as? String, let i = u["index"] as? Int else { return nil }
    return (d, i)
  }

  func filePromiseProvider(_ p: NSFilePromiseProvider, fileNameForType fileType: String) -> String {
    (p.userInfo as? [String: Any])?["name"] as? String ?? "file"
  }

  func filePromiseProvider(_ p: NSFilePromiseProvider, writePromiseTo url: URL, completionHandler: @escaping (Error?) -> Void) {
    guard let (d, i) = info(p) else { return completionHandler(NSError(domain: "TwinDesk", code: 2)) }
    DispatchQueue.main.async { [self] in
      writes["\(d)#\(i)"] = completionHandler
      emit(["event": "dragWrite", "drag": d, "index": i, "path": url.path])
    }
  }

  func operationQueue(for p: NSFilePromiseProvider) -> OperationQueue { queue }

  // ---- drag image: the Finder look (icon over a name label) ----

  private static func thumbnail(_ encoded: String?) -> NSImage? {
    guard let encoded, encoded.utf8.count <= 28000,
          let data = Data(base64Encoded: encoded), data.count <= 20 * 1024,
          let source = CGImageSourceCreateWithData(data as CFData, nil),
          CGImageSourceGetType(source) == UTType.png.identifier as CFString,
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          (1...64).contains(width), (1...64).contains(height),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }
    return NSImage(cgImage: image, size: NSSize(width: width, height: height))
  }

  static func image(name: String, type: UTType, preview: NSImage?) -> NSImage {
    let symbol: String
    if type.conforms(to: .folder) { symbol = "folder" }
    else if type.conforms(to: .image) { symbol = "photo" }
    else if type.conforms(to: .movie) || type.conforms(to: .video) { symbol = "play.rectangle" }
    else if type.conforms(to: .audio) { symbol = "waveform" }
    else if type.conforms(to: .archive) { symbol = "archivebox" }
    else { symbol = "doc.text" }
    let configuration = NSImage.SymbolConfiguration(pointSize: 29, weight: .medium)
      .applying(.init(paletteColors: [NSColor.white]))
    let icon = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?.withSymbolConfiguration(configuration)
      ?? NSWorkspace.shared.icon(for: type)
    let para = NSMutableParagraphStyle()
    para.lineBreakMode = .byTruncatingMiddle
    para.alignment = .center
    let attrs: [NSAttributedString.Key: Any] = [
      .font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor.white, .paragraphStyle: para,
    ]
    let ts = (name as NSString).size(withAttributes: attrs)
    let lw = min(ts.width + 12, 200), lh = ts.height + 4
    let w = max(76, lw + 10), h = 66 + lh
    return NSImage(size: NSSize(width: w, height: h), flipped: false) { _ in
      NSColor(calibratedWhite: 0.12, alpha: 0.92).setFill()
      NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: w, height: h), xRadius: 9, yRadius: 9).fill()
      let content = preview ?? icon
      let edge: CGFloat = preview == nil ? 35 : 53
      let scale = min(edge / content.size.width, edge / content.size.height)
      let size = NSSize(width: content.size.width * scale, height: content.size.height * scale)
      content.draw(in: NSRect(x: (w - size.width) / 2, y: h - (64 + size.height) / 2,
                              width: size.width, height: size.height))
      let r = NSRect(x: (w - lw) / 2, y: 3, width: lw, height: lh)
      (name as NSString).draw(in: r.insetBy(dx: 6, dy: 2), withAttributes: attrs)
      return true
    }
  }
}
