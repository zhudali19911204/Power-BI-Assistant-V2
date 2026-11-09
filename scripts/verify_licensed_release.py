"""Opt-in real EXE/installer checks. No Power BI or model service is contacted."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from PyInstaller.archive.readers import CArchiveReader

from backend.licensing import machine_code, machine_code_from_guid
from scripts.build_desktop import ROOT, find_iscc
from scripts.license_admin import default_key_directory, issue_license, load_private_key


def run(command: list[str], *, success: bool, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, env=env, timeout=120, check=False)
    if (result.returncode == 0) != success:
        raise RuntimeError(f"Unexpected exit code {result.returncode}: {command[0]}")


def check_archive(executable: Path, public_key: bytes) -> None:
    archive = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
    if "_license_trust" not in archive.toc or "scripts.license_admin" in archive.toc:
        raise RuntimeError("Missing embedded public trust or publisher code included in release")
    trust_code = archive.extract("_license_trust")
    if public_key not in trust_code.co_consts:
        raise RuntimeError("Executable does not contain the expected publisher public key")


def native_window(process_id: int) -> int | None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []

    @callback_type
    def collect_window(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(handle):
            windows.append(handle)
        return True

    user32.EnumWindows(collect_window, 0)
    for window in windows:
        renderers = []

        @callback_type
        def collect_renderer(handle, _):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, name, len(name))
            if name.value == "Chrome_RenderWidgetHostHWND":
                renderers.append(handle)
            return True

        user32.EnumChildWindows(window, collect_renderer, 0)
        if renderers:
            return window
    return None


def check_service(executable: Path, env: dict[str, str], *, native: bool = False) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [str(executable), "--port", str(port)]
    if not native:
        command.extend(["--browser", "--no-open"])
    process = subprocess.Popen(command, env=env)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 40
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Licensed desktop exited early: {process.returncode}")
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                    health = json.load(response)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Packaged service did not become ready")
                time.sleep(0.2)
        if health.get("status") != "ok":
            raise RuntimeError("Unexpected packaged health response")
        for resource in ("/", "/assets/app.js", "/assets/vendor/markdown-it.mjs"):
            with opener.open(f"http://127.0.0.1:{port}{resource}", timeout=5) as response:
                if response.status != 200 or not response.read(64):
                    raise RuntimeError(f"Missing packaged frontend resource: {resource}")
        if native:
            window = None
            while process.poll() is None and time.monotonic() < deadline:
                window = native_window(process.pid)
                if window:
                    break
                time.sleep(0.2)
            if not window:
                raise RuntimeError("Packaged native window/WebView2 renderer did not appear")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            if not user32.PostMessageW(window, 0x0010, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if process.wait(timeout=15) != 0:
                raise RuntimeError("Packaged native window did not close cleanly")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--key-dir", type=Path)
    parser.add_argument("--iscc", type=Path)
    args = parser.parse_args()
    release = args.release_dir.resolve()
    private = load_private_key(args.key_dir or default_key_directory())
    public = private.public_key().public_bytes_raw()
    verifier = release / "LicenseVerifier.exe"
    executable = release / "PowerBIAssistant" / "PowerBIAssistant.exe"
    for binary in (verifier, executable):
        check_archive(binary, public)
    forbidden = {".env", "secret.key", "license.key", "license_admin.py"}
    for path in (release / "PowerBIAssistant").rglob("*"):
        if (path.name in forbidden or path.suffix in {".dpapi", ".sqlite3"}
                or path.name.endswith((".private.pem", ".backup.pem"))):
            raise RuntimeError(f"Unexpected publisher/user data in release: {path.name}")
    print("PASS: embedded public keys; no publisher module, private keys or user database")

    name = "LicenseSmoke-" + uuid.uuid4().hex
    test_setup = release / (name + ".exe")
    with tempfile.TemporaryDirectory(prefix="pbi-license-check-") as directory:
        temporary = Path(directory)
        result = temporary / "result.txt"
        valid = temporary / "valid.txt"
        wrong_machine = temporary / "other-machine.txt"
        malformed = temporary / "invalid.txt"
        code = machine_code()
        valid.write_text(issue_license(private, code), encoding="utf-8-sig")
        wrong_machine.write_text(
            issue_license(private, machine_code_from_guid(str(uuid.uuid4()))), encoding="utf-8")
        malformed.write_text("PBI2K-invalid", encoding="utf-8")
        run([str(verifier), "machine-code", "--output", str(result)], success=True)
        if result.read_text(encoding="utf-8") != code:
            raise RuntimeError("Installer helper and runtime disagree about machine identity")
        for path, accepted in ((valid, True), (wrong_machine, False), (malformed, False)):
            run([str(verifier), "verify", "--license-file", str(path), "--output", str(result)],
                success=accepted)
        print("PASS: real installer helper accepts only a valid license for this machine")

        env = dict(os.environ, PBI_DATA_DIR=str(temporary / "unauthorized-data"))
        run([str(executable), "--browser", "--no-open"], success=False, env=env)
        if (temporary / "unauthorized-data" / "assistant.sqlite3").exists():
            raise RuntimeError("Unlicensed release created a database")
        print("PASS: unlicensed packaged startup is denied before service/database creation")

        # A separate AppId prevents the smoke install from modifying a real installation.
        install = temporary / "installed"
        try:
            with (temporary / "compiler.log").open("w", encoding="utf-8") as log:
                subprocess.run(
                    [str(find_iscc(args.iscc)), f"/DReleaseDir={release}", f"/DOutputName={name}",
                     "/DInstallerAppId={{" + str(uuid.uuid4()) + "}",
                     str(ROOT / "packaging" / "installer.iss")],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180,
                )
            common = [str(test_setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/SP-", "/NOICONS", f"/DIR={install}"]
            for path in (None, malformed, wrong_machine):
                command = common + ([f"/LICENSEFILE={path}"] if path else [])
                run(command, success=False)
                if install.exists():
                    raise RuntimeError("Installer wrote application files before authorization")
            print("PASS: missing, invalid and other-machine keys cannot bypass silent installation")
            run(common + [f"/LICENSEFILE={valid}"], success=True)
            installed_exe = install / "PowerBIAssistant.exe"
            if not installed_exe.is_file() or not (install / "license.key").is_file():
                raise RuntimeError("Authorized installer did not persist app and license")
            env = dict(os.environ, PBI_DATA_DIR=str(temporary / "authorized-data"))
            check_service(installed_exe, env)
            print("PASS: authorized install persists license and serves health/UI from the installed EXE")
            check_service(installed_exe, env, native=True)
            print("PASS: licensed native window opens a WebView2 renderer and closes cleanly")
            (install / "license.key").write_text(wrong_machine.read_text(encoding="utf-8"), encoding="utf-8")
            run([str(installed_exe), "--browser", "--no-open"], success=False, env=env)
            (install / "license.key").unlink()
            run([str(installed_exe), "--browser", "--no-open"], success=False, env=env)
            print("PASS: installed app rejects replaced or deleted licenses on subsequent startups")
        finally:
            uninstaller = install / "unins000.exe"
            if uninstaller.is_file():
                run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], success=True)
            test_setup.unlink(missing_ok=True)
    print("LICENSED_RELEASE_OK")


if __name__ == "__main__":
    main()
