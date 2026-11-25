"""Check a fixed release feed and stage a verified installer outside the app directory."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

import httpx

from .engine.net import enable_os_trust_store
from .releases import MANIFEST_LIMIT, Release, UpdateError, require_https, verified_release, version_number


def _https_request(request):
    # httpx calls this hook before every request, including each redirect to a CDN.
    require_https(str(request.url))


def download_chunks(url, limit):
    require_https(url)
    enable_os_trust_store()
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, connect=8),
                          headers={"Accept-Encoding": "identity"},
                          event_hooks={"request": [_https_request]}) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                received = 0
                for chunk in response.iter_bytes(128 * 1024):
                    received += len(chunk)
                    if received > limit:
                        raise UpdateError("更新文件超过声明的大小，请重新检查更新。")
                    yield chunk
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise UpdateError("无法从更新服务器下载文件，请检查网络或稍后重试。") from exc


def _open_for_launch(path):
    if sys.platform != "win32":
        return path.open("rb")
    import _winapi
    import msvcrt

    # Deny writes and renames from verification through CreateProcess.
    file_share_read = 0x00000001
    handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, file_share_read,
                                0, _winapi.OPEN_EXISTING, 0, 0)
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")


def _matches(handle, release):
    return (os.fstat(handle.fileno()).st_size == release.size
            and hashlib.file_digest(handle, "sha256").hexdigest() == release.sha256)


class DesktopUpdates:
    def __init__(self, version, feed, public_key, cache: Path, executable: Path, close_window):
        self.current = version_number(version)
        require_https(feed)
        self.feed, self.public_key = feed, public_key
        self.cache, self.executable, self.close_window = cache, executable, close_window
        self.lock = threading.Lock()
        self.candidate: Release | None = None

    def clear_installed_packages(self):
        # A previous setup may still be finishing when it relaunches the app.
        try:
            for path in self.cache.glob("PowerBIAssistant-Setup-*.exe"):
                match = re.fullmatch(r"PowerBIAssistant-Setup-([0-9.]+)-[0-9a-f]{64}\.exe", path.name)
                if match and version_number(match[1]) <= self.current:
                    path.unlink(missing_ok=True)
        except (OSError, UpdateError):
            pass

    def check(self):
        with self.lock:
            self.candidate = None
            data = b"".join(download_chunks(self.feed, MANIFEST_LIMIT))
            latest = verified_release(data, self.public_key)
            if version_number(latest.version) > self.current:
                self.candidate = latest
                return latest.summary() | {"sha256": latest.sha256}
            return None

    def _selected(self, digest):
        release = self.candidate
        if not release or release.sha256 != digest:
            raise UpdateError("所选版本已变更，请重新检查更新。")
        target = self.cache / f"PowerBIAssistant-Setup-{release.version}-{release.sha256}.exe"
        return release, target

    def download(self, digest):
        with self.lock:
            release, target = self._selected(digest)
            self.cache.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                with target.open("rb") as existing:
                    if _matches(existing, release):
                        return
            checksum = hashlib.sha256()
            output = tempfile.NamedTemporaryFile(dir=self.cache, suffix=".part", delete=False)
            partial = Path(output.name)
            try:
                with output as stream:
                    for chunk in download_chunks(release.url, release.size):
                        stream.write(chunk)
                        checksum.update(chunk)
                    if stream.tell() != release.size or checksum.hexdigest() != release.sha256:
                        raise UpdateError("安装包校验失败，请重新下载。")
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)

    def launch(self, digest):
        with self.lock:
            release, target = self._selected(digest)
            install_dir = self.executable.parent
            with _open_for_launch(target) as package:
                if not _matches(package, release):
                    raise UpdateError("安装包已变更，请重新下载。")
                subprocess.Popen(
                    [str(target), "/SILENT", "/SP-", "/NORESTART", "/NOCLOSEAPPLICATIONS",
                     "/NORESTARTAPPLICATIONS", f"/DIR={install_dir}",
                     f"/LICENSEFILE={install_dir / 'license.key'}", f"/PBIUPDATEPID={os.getpid()}"],
                    cwd=install_dir, env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"),
                )
