"""Native Windows application: JavaScript UI + loopback Python service."""
import argparse
import logging
import os
import socket
import sys
import threading
import time

import uvicorn

from backend.app import create_app, data_directory
from backend.licensing import LicenseError


def main():
    parser = argparse.ArgumentParser(description="Power BI Assistant 桌面版")
    parser.add_argument("--browser", action="store_true", help="在浏览器中预览界面，供开发调试")
    parser.add_argument("--port", type=int, default=0, help="本地端口；默认自动分配")
    parser.add_argument("--no-open", action="store_true", help="浏览器预览时只启动服务")
    args = parser.parse_args()
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PBI_DATA_DIR", str(directory))
    logging.basicConfig(filename=directory / "assistant.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = create_app(directory)
    except LicenseError as exc:
        logging.error("License verification failed: %s", exc)
        if args.browser:
            if sys.stderr is not None:
                print(str(exc), file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Power BI Assistant - 授权验证失败", 0x10)
        return 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(128)
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = uvicorn.Config(app, host="127.0.0.1", log_config=None, access_log=False)
    server = uvicorn.Server(config)
    if args.browser:
        if not args.no_open:
            import webbrowser
            threading.Timer(1, lambda: webbrowser.open(address)).start()
        print(f"Power BI Assistant: {address}", flush=True)
        server.run(sockets=[sock])
        return
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("本地服务未能启动，请查看 assistant.log")
            time.sleep(0.05)
        import webview
        webview.settings['ALLOW_DOWNLOADS']=True
        license_info = app.state.license_info
        title = "Power BI Assistant"
        if license_info and license_info.expires_at is not None:
            title += " - " + license_info.summary
        webview.create_window(title, address, width=1440, height=940,
                              min_size=(1000, 700), background_color="#F8F9FA")
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        logging.exception("Desktop startup failed")
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
