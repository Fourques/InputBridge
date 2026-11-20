import argparse
import os
from pathlib import Path
import sys
import tempfile


def default_profile():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[1] / "local" / "profile"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "InputBridge"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "InputBridge"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "InputBridge"


def main():
    parser = argparse.ArgumentParser(description="InputBridge · quiet file clipboard sharing")
    parser.add_argument("--profile", type=Path, default=default_profile())
    parser.add_argument("--port", type=int, default=24820)
    parser.add_argument("--address", help="Advertised LAN IP when creating a pairing invite")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--preview", type=Path, help="Render the real settings UI offscreen, without reading the system clipboard")
    args = parser.parse_args()
    if args.preview:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .desktop import STYLE, Window, best_lan_address
    from .service import Service
    app = QApplication(sys.argv[:1])
    app.setApplicationName("InputBridge")
    app.setOrganizationName("InputBridge")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    preview_profile = tempfile.TemporaryDirectory(prefix="inputbridge-preview-") if args.preview else None
    profile = Path(preview_profile.name) if preview_profile else args.profile
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(profile / "app.lock"))
    if not lock.tryLock(50):
        QMessageBox.information(None, "InputBridge", "这份配置的 InputBridge 已经在运行，请从托盘打开。")
        return 1
    service = None
    window = None
    try:
        service = Service(profile)
        service.start(host="127.0.0.1" if args.preview else "0.0.0.0",
                      port=0 if args.preview else args.port,
                      advertise_host=args.address or best_lan_address(), polling=not bool(args.preview))
        window = Window(service, clipboard_enabled=not bool(args.preview))
        if not args.preview:
            QTimer.singleShot(0, window.engine.restore)
        if args.preview or not args.background or not service.peers or not window.tray.isVisible():
            window.show()
        if args.preview:
            def capture():
                args.preview.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.preview)):
                    raise RuntimeError("Could not save preview")
                window.quit_app()
            QTimer.singleShot(350, capture)
        return app.exec()
    except Exception as error:
        if args.preview:
            raise
        QMessageBox.critical(None, "InputBridge 启动失败", str(error))
        return 1
    finally:
        if window:
            window.engine.stop()
        if service:
            service.event_wakeup = None
            service.stop()
        lock.unlock()
        if preview_profile:
            preview_profile.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
