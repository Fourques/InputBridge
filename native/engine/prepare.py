"""Reproducible, GUI-free Deskflow core extraction; never reads an installed Deskflow.

Original InputBridge contributions in this generator and its generated patches
are available under the MIT terms in wheel/LICENSE.txt. Deskflow code and
context in the patches retain their upstream licensing and copyright.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / 'upstream.json').read_text())


def prepare(source, refresh_generated=False):
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != PIN['commit']:
        raise RuntimeError('Unexpected upstream commit; refusing to patch')

    buffers = {}
    originals = {}
    manifest_path = source / '.inputbridge-patches.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    def replace(file, old, new):
        if file not in buffers:
            original = subprocess.check_output(['git', '-C', str(source), 'show', 'HEAD:' + file], text=True)
            originals[file] = original
            buffers[file] = original
        s = buffers[file]
        if new in s:
            return
        if s.count(old) != 1:
            raise RuntimeError('Upstream patch context changed: ' + file)
        buffers[file] = s.replace(old, new)

    def add(file, content):
        originals[file] = None
        buffers[file] = content

    replace('CMakeLists.txt', 'option(BUILD_INSTALLER', 'option(BUILD_GUI "Build the upstream settings application" ON)\noption(BUILD_INSTALLER')
    replace('CMakeLists.txt', 'option(BUILD_GUI "Build the upstream settings application" ON)',
            'option(BUILD_WAYLAND_SUPPORT "Build Wayland backends" ON)\noption(BUILD_GUI "Build the upstream settings application" ON)')
    replace('CMakeLists.txt', 'if (WIN32 OR APPLE)\n  find_program(DEPLOYQT',
            'if ((WIN32 OR APPLE) AND BUILD_INSTALLER)\n  find_program(DEPLOYQT')
    replace('CMakeLists.txt', 'add_subdirectory(translations)', 'if(BUILD_GUI)\n  add_subdirectory(translations)\nendif()')
    replace('CMakeLists.txt', 'set(CMAKE_AUTOMOC ON)', '''if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options("-ffile-prefix-map=${CMAKE_SOURCE_DIR}=deskflow"
                      "-ffile-prefix-map=${CMAKE_BINARY_DIR}=deskflow-build")
endif()
set(CMAKE_AUTOMOC ON)''')
    replace('src/lib/CMakeLists.txt', 'add_subdirectory(gui)', 'if(BUILD_GUI)\n  add_subdirectory(gui)\nendif()')
    replace('src/apps/CMakeLists.txt', 'add_subdirectory(deskflow-gui)', 'if(BUILD_GUI)\n  add_subdirectory(deskflow-gui)\nendif()')
    replace('src/lib/platform/CMakeLists.txt', 'if(UNIX AND NOT APPLE)\n  pkg_check_modules(LIBEI',
            'if(UNIX AND NOT APPLE AND BUILD_WAYLAND_SUPPORT)\n  pkg_check_modules(LIBEI')
    replace('src/lib/platform/CMakeLists.txt', '    target_compile_definitions(platform PUBLIC WINAPI_LIBEI WINAPI_LIBPORTAL)',
            '    if(LIBEI_FOUND)\n      target_compile_definitions(platform PUBLIC WINAPI_LIBEI)\n    endif()\n'
            '    if(LIBPORTAL_FOUND AND LIBEI_FOUND)\n      target_compile_definitions(platform PUBLIC WINAPI_LIBPORTAL)\n    endif()')
    replace('src/apps/deskflow-core/CMakeLists.txt', 'add_executable(${target}',
            'add_executable(${target}')  # target source name remains upstream's
    replace('src/apps/deskflow-core/CMakeLists.txt', 'target_link_libraries(\n',
            'set_target_properties(${target} PROPERTIES OUTPUT_NAME "inputbridge-core")\n\ntarget_link_libraries(\n')
    constants = 'src/lib/common/Constants.h.in'
    replace(constants, 'const auto kAppName = "@CMAKE_PROJECT_PROPER_NAME@";', 'const auto kAppName = "InputBridgeEngine";')
    replace(constants, 'const auto kAppId = "@CMAKE_PROJECT_NAME@";', 'const auto kAppId = "inputbridge-engine";')
    replace(constants, 'const auto kCoreBinName = "@CORE_BINARY@";', 'const auto kCoreBinName = "inputbridge-core";')
    replace(constants, 'const auto kCoreIpcName = "@CMAKE_PROJECT_NAME@-core";', 'const auto kCoreIpcName = "inputbridge-core";')
    # State and settings are private to our profile; no Deskflow defaults or IPC.
    replace('src/lib/common/Settings.cpp', 'const auto stateFile = QStringLiteral("%1/%2.state").arg(stateBase, kAppName);',
            'const auto stateFile = QStringLiteral("%1/InputBridgeEngine.state").arg(qEnvironmentVariable("INPUTBRIDGE_ENGINE_HOME", stateBase));')
    replace('src/lib/common/Settings.cpp', '  m_settings = new QSettings(fileToLoad, QSettings::IniFormat, this);',
            '  if (const auto home = qEnvironmentVariable("INPUTBRIDGE_ENGINE_HOME"); !home.isEmpty())\n'
            '    fileToLoad = home + "/core.ini";\n  m_settings = new QSettings(fileToLoad, QSettings::IniFormat, this);')
    replace('src/lib/deskflow/ipc/IpcServer.cpp', 'QLocalServer::WorldAccessOption', 'QLocalServer::UserAccessOption')
    replace('src/apps/deskflow-core/deskflow-core.cpp', '#include "deskflow/ServerApp.h"',
            '#include "deskflow/ServerApp.h"\n#include "common/Settings.h"\n#include "server/Config.h"\n#include <fstream>')
    replace('src/apps/deskflow-core/deskflow-core.cpp', '#include <QSharedMemory>',
            '#include <QScopeGuard>\n#include <QSharedMemory>')
    replace('src/apps/deskflow-core/deskflow-core.cpp', '  qInstallMessageHandler(qtMessageHandler);',
            '  const auto restoreQtHandler = qScopeGuard([previous = qInstallMessageHandler(qtMessageHandler)] {\n'
            '    qInstallMessageHandler(previous);\n'
            '  });')
    replace('src/apps/deskflow-core/deskflow-core.cpp', '  CoreArgParser parser(QCoreApplication::arguments());', '''  if (app.arguments().size() == 3 && app.arguments().at(1) == "--validate-config") {
    try {
      Settings::setSettingsFile(QFileInfo(app.arguments().at(2)).absolutePath() + "/core.ini");
      EventQueue configEvents;
      deskflow::server::Config config(&configEvents);
      std::ifstream stream(app.arguments().at(2).toStdString());
      if (!stream) return 2;
      stream >> config;
      QTextStream(stdout) << "configuration valid\\n";
      return 0;
    } catch (const std::exception &) {
      QTextStream(stderr) << "configuration invalid\\n";
      return 2;
    }
  }
  CoreArgParser parser(QCoreApplication::arguments());''')
    replace('src/lib/arch/unix/ArchNetworkBSD.cpp', 'return std::make_shared<struct pollfd[]>(n);',
            'return std::shared_ptr<struct pollfd[]>(new pollfd[n]()); // also supports libstdc++ 11')
    replace('src/lib/deskflow/ipc/CoreIpcServer.h', '#include <QSet>', '#include <QSet>\n#include <atomic>')
    replace('src/lib/deskflow/ipc/CoreIpcServer.h', '  static CoreIpcServer &instance();',
            '  static CoreIpcServer &instance();\n  bool fileDragAllowed() const;\n'
            '  void disarmFileDrag() { m_dragUntil.store(0); m_dragReturnUntil.store(0); }\n'
            '  void allowFileDragReturn();\n  bool fileDragReturnAllowed() const;\n'
            '  unsigned int wheelOptions() const { return m_wheel.load(); }')
    replace('src/lib/deskflow/ipc/CoreIpcServer.h', 'private:\n',
            'private:\n  std::atomic<long long> m_dragUntil{0};\n'
            '  std::atomic<long long> m_dragReturnUntil{0};\n  std::atomic_uint m_wheel{0};\n')
    replace('src/lib/deskflow/ipc/CoreIpcServer.cpp', '#include <QLocalSocket>', '#include <QLocalSocket>\n#include <chrono>')
    replace('src/lib/deskflow/ipc/CoreIpcServer.cpp', '  s_instance = this;',
            '  s_instance = this;\n  const auto wheel = qEnvironmentVariableIntValue("INPUTBRIDGE_WHEEL_OPTIONS");\n'
            '  if (wheel >= 0 && wheel < (1 << 28)) m_wheel.store(static_cast<unsigned int>(wheel));')
    replace('src/lib/deskflow/ipc/CoreIpcServer.cpp', 'void CoreIpcServer::processCommand', '''bool CoreIpcServer::fileDragAllowed() const
{
  const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
  return now < m_dragUntil.load();
}

void CoreIpcServer::allowFileDragReturn()
{
  const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
  m_dragReturnUntil.store(now + 90000);
}

bool CoreIpcServer::fileDragReturnAllowed() const
{
  const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
  return now < m_dragReturnUntil.load();
}

void CoreIpcServer::processCommand''')
    replace('src/lib/deskflow/ipc/CoreIpcServer.cpp', '  Q_UNUSED(parts)', '''  if (command == QStringLiteral("file-drag")) {
    const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
    if (parts.value(1) == "1") m_dragUntil.store(now + 1500);
    else disarmFileDrag();
    writeToClientSocket(clientSocket, QStringLiteral("ok"));
    return;
  }
  if (command == QStringLiteral("wheel")) {
    bool valid = false;
    const auto bits = parts.value(1).toUInt(&valid);
    if (valid && bits < (1u << 28)) {
      m_wheel.store(bits);
      writeToClientSocket(clientSocket, QStringLiteral("ok"));
    } else {
      writeToClientSocket(clientSocket, QStringLiteral("error"));
    }
    return;
  }
  Q_UNUSED(parts)''')
    # Only focus and button metadata, never keyboard data, travels over the private stdout pipe.
    replace('src/lib/server/Server.cpp', '#include "server/Server.h"', '''#include "server/Server.h"
#include "deskflow/ipc/CoreIpcServer.h"
#include <QJsonDocument>
#include <QJsonObject>
#include <cstdio>
static void inputbridgeEvent(const QJsonObject &event)
{
  if (!qEnvironmentVariableIsSet("INPUTBRIDGE_EVENTS")) return;
  const auto data = QJsonDocument(event).toJson(QJsonDocument::Compact);
  fprintf(stdout, "IB_EVENT %s\\n", data.constData());
  fflush(stdout);
}''')
    for owner, path in [('Computer', 'src/lib/deskflow/Computer'), ('PrimaryClient', 'src/lib/server/PrimaryClient')]:
        replace(path + '.h', 'bool isLockedToComputer() const;', 'bool isLockedToComputer(uint32_t *button = nullptr) const;')
        replace(path + '.cpp', f'bool {owner}::isLockedToComputer() const',
                f'bool {owner}::isLockedToComputer(uint32_t *button) const')
    replace('src/lib/deskflow/Computer.cpp', '    LOG_DEBUG("locked by mouse buttonID: %d", buttonID);',
            '    if (button) *button = buttonID;\n    LOG_DEBUG("locked by mouse buttonID: %d", buttonID);')
    replace('src/lib/server/PrimaryClient.cpp', 'return m_computer->isLockedToComputer();',
            'return m_computer->isLockedToComputer(button);')
    x11 = 'src/lib/platform/XWindowsComputer.cpp'
    replace(x11, 'bool XWindowsComputer::isAnyMouseButtonDown(uint32_t &) const',
            'bool XWindowsComputer::isAnyMouseButtonDown(uint32_t &button) const')
    replace(x11, '    return ((state & (Button1Mask | Button2Mask | Button3Mask | Button4Mask | Button5Mask)) != 0);',
            '''    const auto held = state & (Button1Mask | Button2Mask | Button3Mask | Button4Mask | Button5Mask);
    button = held == Button1Mask ? kButtonLeft : kButtonNone;
    return held != 0;''')
    replace('src/lib/server/Server.h', '  bool m_enableClipboard = true;',
            '  uint32_t m_inputbridgeButtons = 0;\n'
            '  BaseClientProxy *m_inputbridgeDragOrigin = nullptr;\n'
            '  bool m_inputbridgeDragHorizontal = false;\n'
            '  bool m_inputbridgeDragCanReturn = false;\n'
            '  bool m_enableClipboard = true;')
    replace('src/lib/server/Server.h', '  bool isLockedToComputer() const;',
            '  bool isLockedToComputer() const;\n  uint32_t inputbridgeButtons() const;\n'
            '  void updateFileDragReturn(int32_t x, int32_t y);')
    replace('src/lib/server/Server.cpp', 'bool Server::isLockedToComputer() const', '''uint32_t Server::inputbridgeButtons() const
{
#if defined(Q_OS_LINUX)
  // X11 apps can own the release event after the pointer returns to the primary screen.
  uint32_t button = 0;
  if (!m_primaryClient->isLockedToComputer(&button)) return 0;
  return button == 1 ? (1u << 1) : 1u;
#else
  return m_inputbridgeButtons;
#endif
}

bool Server::isLockedToComputer() const''')
    replace('src/lib/server/Server.cpp', 'int32_t Server::getJumpZoneSize', '''void Server::updateFileDragReturn(int32_t x, int32_t y)
{
  if (!m_inputbridgeDragOrigin || m_inputbridgeDragCanReturn ||
      !deskflow::core::ipc::CoreIpcServer::instance().fileDragReturnAllowed())
    return;
  int32_t ax, ay, aw, ah;
  m_active->getShape(ax, ay, aw, ah);
  // Move off the starting edge before allowing a deliberate trip back.
  constexpr int margin = 32;
  m_inputbridgeDragCanReturn = m_inputbridgeDragHorizontal
      ? x >= ax + margin && x < ax + aw - margin
      : y >= ay + margin && y < ay + ah - margin;
}

int32_t Server::getJumpZoneSize''')
    replace('src/lib/server/Server.cpp', '''  // ignore if mouse is locked to computer and don't try to switch later
  if (!preventSwitch && isLockedToComputer()) {''', '''  auto &dragIpc = deskflow::core::ipc::CoreIpcServer::instance();
  const bool returningFile = m_inputbridgeDragCanReturn && newComputer == m_inputbridgeDragOrigin &&
      dragIpc.fileDragReturnAllowed() && inputbridgeButtons() == (1u << 1);
  if (!preventSwitch && !returningFile && isLockedToComputer()) {''')
    replace('src/lib/server/Server.cpp', '''  return !preventSwitch;
}

void Server::noSwitch''', '''  if (!preventSwitch && !returningFile && dragIpc.fileDragAllowed())
    m_inputbridgeDragHorizontal = dir == Direction::Left || dir == Direction::Right;
  return !preventSwitch;
}

void Server::noSwitch''')
    replace('src/lib/server/Server.cpp', '  m_x = x;\n  m_y = y;\n\n  // get computer shape',
            '  m_x = x;\n  m_y = y;\n  updateFileDragReturn(x, y);\n\n  // get computer shape')
    replace('src/lib/server/Server.cpp', '  m_x += dx;\n  m_y += dy;\n\n  // get computer shape',
            '  m_x += dx;\n  m_y += dy;\n  updateFileDragReturn(m_x, m_y);\n\n  // get computer shape')
    replace('src/lib/server/Server.cpp', '  if (m_primaryClient->isLockedToComputer()) {', '''  const auto buttons = inputbridgeButtons();
#if defined(Q_OS_MACOS) || defined(Q_OS_LINUX)
  // AppKit's synthetic drag press must not lock the hardware pointer to a screen.
  const bool held = buttons != 0;
#else
  const bool held = buttons != 0 || m_primaryClient->isLockedToComputer();
#endif
  const bool fileHandoff = buttons == (1u << 1) &&
      deskflow::core::ipc::CoreIpcServer::instance().fileDragAllowed();
  if (held && !fileHandoff) {''')
    # The native receiver owns the initial press. Replaying it here delivers it
    # into the desktop before its drag source is mapped and steals the X11 grab.
    # Primary snapshots are refreshed on leave while a file button is down, but
    # never track subsequent hardware releases; only secondary fake input uses
    # that cache. Consult live button state on the primary to avoid a sticky lock.
    replace('src/lib/platform/OSXComputer.mm', '  if (m_buttonState.test(0)) {',
            '  if (!m_isPrimary && m_buttonState.test(0)) {')
    replace('src/lib/platform/OSXComputer.mm',
            'OSXComputer::handleCGInputEvent(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *refcon)\n{',
            'OSXComputer::handleCGInputEvent(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *refcon)\n{\n'
            '  if (event && CGEventGetIntegerValueField(event, kCGEventSourceUserData) == 0x49425247) return event;')
    # Separate physical wheels before the remote protocol drops the source.
    platform = 'src/lib/platform/'
    # Give the application exclusive ownership of file/image clipboard formats.
    # The keyboard core continues forwarding text and HTML separately.
    replace('src/lib/deskflow/IClipboard.h', 'static bool copy(IClipboard *dst, const IClipboard *src, Time);',
            'static bool copy(IClipboard *dst, const IClipboard *src, Time, bool textOnly = false);')
    clipboard = 'src/lib/deskflow/IClipboard.cpp'
    replace(clipboard, 'bool IClipboard::copy(IClipboard *dst, const IClipboard *src, Time time)',
            'bool IClipboard::copy(IClipboard *dst, const IClipboard *src, Time time, bool textOnly)')
    replace(clipboard, '  if (src->open(time)) {\n    if (dst->open(time)) {',
            '''  if (src->open(time)) {
    if (textOnly && !src->has(Format::Text) && !src->has(Format::HTML)) {
      src->close();
      return true;
    }
    if (dst->open(time)) {''')
    replace(clipboard, '          if (src->has(eFormat)) {',
            '          if ((!textOnly || eFormat != Format::Bitmap) && src->has(eFormat)) {')
    replace(platform + 'OSXClipboard.h', '  bool synchronize();',
            '  bool synchronize();\n  bool hasAsset() const;')
    replace(platform + 'OSXClipboard.cpp', 'bool OSXClipboard::synchronize()', '''bool OSXClipboard::hasAsset() const
{
  PasteboardItemID item;
  if (!m_pboard || PasteboardGetItemIdentifier(m_pboard, 1, &item) != noErr)
    return false;

  CFArrayRef flavors = nullptr;
  if (PasteboardCopyItemFlavors(m_pboard, item, &flavors) != noErr || !flavors)
    return false;
  bool found = false;
  for (CFIndex i = 0; i < CFArrayGetCount(flavors) && !found; ++i) {
    auto flavor = static_cast<CFStringRef>(CFArrayGetValueAtIndex(flavors, i));
    found = CFEqual(flavor, CFSTR("public.file-url")) ||
        CFEqual(flavor, CFSTR("com.trolltech.anymime.application--x-inputbridge-origin")) ||
        UTTypeConformsTo(flavor, kUTTypeImage);
  }
  CFRelease(flavors);
  return found;
}

bool OSXClipboard::synchronize()''')
    replace(platform + 'OSXComputer.mm', '  Clipboard::copy(dst, &m_pasteboard);',
            '''  if (qEnvironmentVariableIsSet("INPUTBRIDGE_ASSET_CLIPBOARD") && m_pasteboard.hasAsset()) {
    if (!dst->open(m_pasteboard.getTime())) return false;
    dst->empty();
    dst->close();
    return true;
  }
  Clipboard::copy(dst, &m_pasteboard);''')
    replace(platform + 'OSXComputer.mm', '    Clipboard::copy(&m_pasteboard, src);',
            '''    Clipboard::copy(&m_pasteboard, src, src->getTime(),
                    qEnvironmentVariableIsSet("INPUTBRIDGE_ASSET_CLIPBOARD"));''')
    replace(platform + 'XWindowsClipboard.cpp',
            '#include <X11/Xatom.h>', '#include <QtGlobal>\n#include <X11/Xatom.h>')
    x11_targets = '  LOG_DEBUG("  available targets: %s", XWindowsUtil::atomsToString(m_display, targets, numTargets).c_str());'
    replace(platform + 'XWindowsClipboard.cpp', x11_targets, x11_targets + '''
  if (m_id == kClipboardClipboard && qEnvironmentVariableIsSet("INPUTBRIDGE_ASSET_CLIPBOARD")) {
    const char *const assetTypes[] = {"application/x-inputbridge-origin", "x-special/gnome-copied-files",
        "application/x-kde-cutselection", "image/png", "image/jpeg", "image/webp", "image/tiff",
        "image/gif", "image/bmp", "image/x-bmp", "application/x-qt-image"};
    for (const auto type : assetTypes) {
      const auto atom = XInternAtom(m_display, type, True);
      if (atom != None && std::find(targets, targets + numTargets, atom) != targets + numTargets)
        return;
    }
  }''')
    replace(platform + 'XWindowsComputer.cpp',
            '#include <X11/X.h>', '#include <QtGlobal>\n#include <X11/X.h>')
    replace(platform + 'XWindowsComputer.cpp',
            'bool XWindowsComputer::setClipboard(ClipboardID id, const IClipboard *clipboard)\n{',
            '''bool XWindowsComputer::setClipboard(ClipboardID id, const IClipboard *clipboard)
{
  const bool textOnly = id == kClipboardClipboard && qEnvironmentVariableIsSet("INPUTBRIDGE_ASSET_CLIPBOARD");
  if (textOnly && !clipboard) return true;
''')
    replace(platform + 'XWindowsComputer.cpp', '    return Clipboard::copy(m_clipboard[id], clipboard, timestamp);',
            '    return Clipboard::copy(m_clipboard[id], clipboard, timestamp, textOnly);')
    for name in ['WheelMotion.h', 'MacWheel.h']:
        add(platform + 'InputBridge' + name, (HERE / 'wheel' / name).read_text())
    replace(platform + 'OSXComputer.h', '#include <bitset>',
            '#include "platform/InputBridgeMacWheel.h"\n#include <bitset>')
    replace(platform + 'OSXComputer.h', '  bool m_isOnComputer;',
            '  std::atomic_bool m_isOnComputer;\n  std::atomic_uint m_wheelEpoch{0};\n'
            '  inputbridge::MacWheel m_inputbridgeWheel;')
    mac = platform + 'OSXComputer.mm'
    replace(mac, '        CFRunLoopRun();\n        CFRunLoopRemoveSource',
            '        CFRunLoopRun();\n        m_inputbridgeWheel.cancel();\n        CFRunLoopRemoveSource')
    replace(mac, '  m_isOnComputer = true;\n  showCursor();',
            '  ++m_wheelEpoch;\n  m_isOnComputer = true;\n  showCursor();')
    replace(mac, '  // now off computer\n  m_isOnComputer = false;',
            '  // now off computer\n  m_isOnComputer = false;\n  ++m_wheelEpoch;')
    replace(mac,
            '''  case kCGEventScrollWheel:
    computer->onMouseWheel(
        computer->mapScrollWheelToDeskflow(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2)),
        computer->mapScrollWheelToDeskflow(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1))
    );
    break;''',
            '''  case kCGEventScrollWheel: {
    using inputbridge::MacWheel;
    using inputbridge::WheelOptions;
    if (!event) break;
    if (MacWheel::generated(event))
      return computer->m_isOnComputer && MacWheel::sameFocus(event, computer->m_wheelEpoch.load()) ? event : nullptr;
    const WheelOptions options{deskflow::core::ipc::CoreIpcServer::instance().wheelOptions()};
    const bool local = computer->m_isOnComputer;
    const bool adjust = (local ? options.local() : options.away()) &&
        MacWheel::eligible(event) && !MacWheel::gestureInProgress(event);
    if (adjust && local) {
      if (computer->m_inputbridgeWheel.local(event, options, computer->m_isOnComputer, computer->m_wheelEpoch))
        return nullptr;
      // A locally adjusted wheel should never be reinterpreted by a new peer.
      return event;
    } else {
      computer->m_inputbridgeWheel.cancel();
    }
    const auto x = computer->mapScrollWheelToDeskflow(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2));
    const auto y = computer->mapScrollWheelToDeskflow(CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1));
    computer->onMouseWheel(inputbridge::directed(x, adjust && !local && options.has(WheelOptions::AwayX)),
                           inputbridge::directed(y, adjust && !local && options.has(WheelOptions::AwayY)));
    break;
  }''')
    replace(mac, '''  case kCGEventTapDisabledByTimeout:
    // Re-enable our event-tap''', '''  case kCGEventTapDisabledByTimeout:
    computer->m_inputbridgeWheel.cancel();
    // Re-enable our event-tap''')
    replace(mac, '''  case kCGEventOtherMouseDown:
    computer->onMouseButton(true''', '''  case kCGEventOtherMouseDown:
    computer->m_inputbridgeWheel.cancel();
    computer->onMouseButton(true''')
    replace('src/lib/server/Server.cpp', '    // leave active computer',
            '    const auto inputbridgeHeldButtons = inputbridgeButtons();\n    // leave active computer')
    replace('src/lib/server/Server.cpp', '    // cut over\n    m_active = dst;',
            '''    const auto inputbridgePrevious = getName(m_active);
    auto &dragIpc = deskflow::core::ipc::CoreIpcServer::instance();
    const bool forwardFile = dragIpc.fileDragAllowed() && inputbridgeHeldButtons == (1u << 1);
    dragIpc.disarmFileDrag();
    m_inputbridgeDragOrigin = forwardFile ? m_active : nullptr;
    m_inputbridgeDragCanReturn = false;
    if (forwardFile) dragIpc.allowFileDragReturn();
    // cut over
    m_active = dst;''')
    replace('src/lib/server/Server.cpp', '    // increment enter sequence number', '''    inputbridgeEvent({{"event", "focus"}, {"from", QString::fromStdString(inputbridgePrevious)},
      {"to", QString::fromStdString(getName(dst))}, {"x", x}, {"y", y},
      {"buttons", static_cast<int>(inputbridgeHeldButtons)}});

    // increment enter sequence number''')
    replace('src/lib/server/Server.cpp', '  LOG_VERBOSE("onMouseDown id=%d", id);',
            '  if (id < 32) m_inputbridgeButtons |= (1u << id);\n  LOG_VERBOSE("onMouseDown id=%d", id);')
    replace('src/lib/server/Server.cpp', '  LOG_VERBOSE("onMouseUp id=%d", id);', '''  if (id < 32) m_inputbridgeButtons &= ~(1u << id);
  if (id == 1) deskflow::core::ipc::CoreIpcServer::instance().disarmFileDrag();
  inputbridgeEvent({{"event", "button-up"}, {"button", static_cast<int>(id)},
    {"screen", QString::fromStdString(getName(m_active))}, {"x", m_x}, {"y", m_y}});
  LOG_VERBOSE("onMouseUp id=%d", id);''')
    license_name = 'LICENSE.InputBridge-MIT.txt'
    add(license_name, (HERE / 'wheel' / 'LICENSE.txt').read_text())
    for file, content in list(buffers.items()):
        if originals[file] is not None and content != originals[file]:
            comment = '#' if file.endswith('CMakeLists.txt') else '//'
            buffers[file] = (
                f'{comment} Modified by InputBridge contributors on 2026-11-18 for the embedded input engine.\n'
                f'{comment} Original InputBridge contributions: MIT; see {license_name}.\n'
                f'{comment} Upstream portions retain their existing SPDX notices.\n\n' + content
            )
    for file, content in buffers.items():
        path = source / file
        current = path.read_text() if path.exists() else None
        approved = current is not None and manifest.get(file) == hashlib.sha256(current.encode()).hexdigest()
        if current not in (originals[file], content) and not approved and not refresh_generated:
            raise RuntimeError('Unrecognized edits; not overwriting ' + file)
    for file, content in buffers.items():
        (source / file).write_text(content)
    manifest_path.write_text(json.dumps({f: hashlib.sha256(s.encode()).hexdigest() for f, s in buffers.items()}, indent=2))
    print('Prepared pinned input core without upstream GUI:', actual)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--refresh-generated', action='store_true')
    args = parser.parse_args()
    prepare(args.source, args.refresh_generated)
