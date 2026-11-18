// InputBridge native drag bridge. GPL-3.0-or-later; see bundled source notices.
import Cocoa
import ApplicationServices

let outputLock = NSLock()
func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
    outputLock.lock()
    FileHandle.standardOutput.write(data + Data([10]))
    outputLock.unlock()
}
func log(_ message: String) { /* Native event contents are never written to a log. */ }

final class Injector {
    var heldButtons = Set<Int>()
    private var serial: Int64 = 1
    func mouse(_ type: CGEventType, at point: CGPoint, button: Int, clickState: Int) {
        guard let b = CGMouseButton(rawValue: UInt32(button)),
              let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: b) else { return }
        event.setIntegerValueField(.eventSourceUserData, value: 0x49425247)
        event.setIntegerValueField(.mouseEventClickState, value: Int64(clickState))
        event.setIntegerValueField(.mouseEventNumber, value: serial)
        serial += 1
        event.post(tap: .cghidEventTap)
    }
    func button(_ button: Int, down: Bool, at point: CGPoint) {
        if down { heldButtons.insert(button) } else { heldButtons.remove(button) }
        let type: CGEventType = button == 0 ? (down ? .leftMouseDown : .leftMouseUp) : (down ? .rightMouseDown : .rightMouseUp)
        mouse(type, at: point, button: button, clickState: 1)
    }
}
let injector = Injector()

if CommandLine.arguments.contains("--request-accessibility") {
    let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
    emit(["accessibilityGranted": AXIsProcessTrustedWithOptions([key: true] as CFDictionary)])
    exit(0)
}
if CommandLine.arguments.contains("--capabilities") {
    emit(["protocol": 1, "filePromises": true, "directories": true, "copyOnly": true,
          "nativeDestination": true, "accessibilityGranted": AXIsProcessTrusted()])
    exit(0)
}

func safeName(_ name: String) -> Bool {
    !name.isEmpty && name != "." && name != ".." && name.utf8.count <= 1024 &&
      !name.contains("/") && !name.contains("\\") && !name.unicodeScalars.contains(where: { $0.value < 32 })
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
var input = Data()
let reader = FileHandle.standardInput
reader.readabilityHandler = { handle in
    let chunk = handle.availableData
    if chunk.isEmpty { DispatchQueue.main.async { app.terminate(nil) }; return }
    input.append(chunk)
    if input.count > 65536 { input.removeAll(); return }
    while let end = input.firstIndex(of: 10) {
        let line = input[..<end]
        input.removeSubrange(...end)
        guard let command = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
        DispatchQueue.main.async {
            guard let op = command["op"] as? String else { return }
            if op == "stop" { DragIn.shared.cancel(); EdgeCapture.shared.stop(); app.terminate(nil); return }
            if op == "edges", let side = command["side"] as? String {
                EdgeCapture.shared.configure(side: side); return
            }
            guard let drag = command["drag"] as? String,
                  drag.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { return }
            switch op {
            case "start":
                guard AXIsProcessTrusted(), let items = command["items"] as? [[String: Any]],
                      !items.isEmpty && items.count <= 64,
                      items.allSatisfy({ safeName(($0["name"] as? String) ?? "") }) else {
                    emit(["event": "dragEnded", "drag": drag, "result": "failed"]); return
                }
                DragIn.shared.start(drag: drag, items: items)
            case "written":
                if let index = command["index"] as? Int {
                    DragIn.shared.written(drag: drag, index: index, error: command["error"] as? String)
                }
            case "cancel": DragIn.shared.cancel()
            case "buttonUp": _ = DragIn.shared.buttonUp()
            case "swallow":
                if AXIsProcessTrusted() { DragIn.shared.swallowLocalDrag(at: CGEvent(source: nil)?.location ?? .zero) }
            default: break
            }
        }
    }
}
app.run()
