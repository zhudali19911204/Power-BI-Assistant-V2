"""Opt-in native-window startup check. It closes the test window after loading.

Run manually: .venv/Scripts/python.exe -m tests.smoke_desktop
This only manages the native window lifecycle; it does not automate browser content.
"""
import sys
import threading

import webview

import desktop


def main():
    original = webview.create_window
    loaded = threading.Event()
    def create_window(*args, **kwargs):
        window = original(*args, **kwargs)
        def on_loaded():
            loaded.set()
            print('NATIVE_WINDOW_LOADED', flush=True)
            threading.Timer(2, window.destroy).start()
        window.events.loaded += on_loaded
        # Close even if the renderer fails to load the local document.
        watchdog = threading.Timer(25, window.destroy)
        watchdog.daemon = True
        watchdog.start()
        return window
    webview.create_window = create_window
    sys.argv = ['desktop.py']
    desktop.main()
    if not loaded.is_set():
        raise RuntimeError('Native document did not load')
    print('NATIVE_STARTUP_OK', flush=True)


if __name__ == '__main__':
    main()
