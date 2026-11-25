"""Opt-in Windows OTA test using disposable keys, two packaged builds and a loopback HTTPS feed."""
import ctypes
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
import httpx

from backend.licensing import machine_code
from scripts.build_desktop import ROOT, find_iscc
from scripts.license_admin import issue_license
from scripts.sign_update import sign_installer
from scripts.verify_licensed_release import native_window


def local_https(directory):
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Power BI Assistant local update test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(tls_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(tls_key, hashes.SHA256()))
    cert_file, key_file = directory / "test-ca.pem", directory / "test-tls.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(tls_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    public = directory / "web"
    public.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(public)))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server, cert_file


def wait_for(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.25)
    raise RuntimeError(f"Timed out waiting for {check.__name__}")


def listening_port(pid):
    rows = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, errors="replace")
    for row in rows.splitlines():
        parts = row.split()
        if (len(parts) == 5 and parts[-1] == str(pid) and parts[1].startswith("127.0.0.1:")
                and parts[2] == "0.0.0.0:0"):
            return int(parts[1].rsplit(":", 1)[1])


def exercise(directory, server, cert_file):
    base = f"https://127.0.0.1:{server.server_port}"
    feed = base + "/latest.json"
    old, new = "2.0.13.1", "2.0.13.2"
    signing = ed25519.Ed25519PrivateKey.generate()
    public_key = directory / "public.pem"
    public_key.write_bytes(signing.public_key().public_bytes(serialization.Encoding.PEM,
                                                            serialization.PublicFormat.SubjectPublicKeyInfo))
    identity = str(uuid.uuid4())
    profile, install = directory / "profile", directory / "installed desktop"
    cache = directory / "temp"
    cache.mkdir()
    env = dict(os.environ, SSL_CERT_FILE=str(cert_file), NO_PROXY="127.0.0.1,localhost",
               PBI_DATA_DIR=str(profile), TMP=str(cache), TEMP=str(cache),
               PYINSTALLER_CONFIG_DIR=str(directory / "pyinstaller-cache"))
    packages = []
    for version in (old, new):
        built = directory / version
        with (directory / f"{version}.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, "-m", "scripts.build_desktop", "--version", version, "--public-key",
                            str(public_key), "--update-feed", feed, "--output-dir", str(built),
                            "--work-dir", str(directory / "work" / version)],
                           cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run([str(find_iscc(None)), f"/DReleaseDir={built}", f"/DAppVersion={version}",
                            "/DInstallerAppId={{" + identity + "}", f"/O{directory / 'web'}",
                            str(ROOT / "packaging/installer.iss")], stdout=log, stderr=subprocess.STDOUT, check=True)
        packages.append(directory / "web" / f"PowerBIAssistant-Setup-{version}.exe")
        print(f"Built test installer {version}", flush=True)
    manifest = directory / "web/latest.json"
    manifest.write_bytes(sign_installer(packages[1], signing, url=f"{base}/{packages[1].name}", notes="升级验证"))
    key = directory / "test.license"
    key.write_text(issue_license(signing, machine_code(), license_type="trial"), encoding="utf-8")
    source = None
    restarted_pid = None
    try:
        subprocess.run([str(packages[0]), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-", "/NOICONS",
                        f"/DIR={install}", f"/GROUP=PBI-Update-Test-{identity}", f"/LICENSEFILE={key}"],
                       check=True, env=env, timeout=120)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        source = subprocess.Popen([str(install / "PowerBIAssistant.exe"), "--port", str(port)], env=env)
        wait_for(lambda: native_window(source.pid))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/", trust_env=False, timeout=60) as api:
            assert api.get("updates").json() == {"enabled": True, "version": old}
            trial = api.get("health").json()["license"]
            model = api.post("models/demo", json={}).json()
            conversation = api.post("conversations", json={"model_id": model["id"], "mode": "dax"}).json()["id"]
            api.put("ui/layout", json={"context_width": 333, "sidebar_width": 234}).raise_for_status()
            credential = (profile / "secret.key").read_bytes()
            license_bytes = (install / "license.key").read_bytes()
            release = api.post("updates/check", json={}).raise_for_status().json()["release"]
            assert release["version"] == new
            target = f"updates/{release['sha256']}"
            api.post(target + "/download", json={}).raise_for_status()
            api.post(target + "/install", json={}).raise_for_status()
        assert source.wait(timeout=60) == 0

        def new_process():
            log = (profile / "assistant.log").read_text(encoding="utf-8", errors="replace")
            pids = re.findall(r"Started server process \[(\d+)\]", log)
            return int(pids[-1]) if pids and int(pids[-1]) != source.pid else None

        restarted_pid = wait_for(new_process, 120)
        wait_for(lambda: native_window(restarted_pid))
        new_port = wait_for(lambda: listening_port(restarted_pid))
        with httpx.Client(base_url=f"http://127.0.0.1:{new_port}/api/", trust_env=False) as api:
            health = api.get("health").json()
            assert health["version"] == new and health["license"] == trial
            assert api.get("ui/layout").json() == {"context_width":333, "sidebar_width":234}
            assert any(c["id"] == conversation for c in api.get("conversations").json())
            assert api.post("updates/check", json={}).json() == {"release":None}
        assert (profile / "secret.key").read_bytes() == credential
        assert (install / "license.key").read_bytes() == license_bytes
        print("PASS: signed HTTPS update relaunched the new native binary and preserved trial, data and credentials")
    finally:
        if restarted_pid:
            import _winapi
            process = _winapi.OpenProcess(0x00100000, False, restarted_pid)  # SYNCHRONIZE
            handle = native_window(restarted_pid)
            try:
                if handle:
                    ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), 0x10, 0, 0)
                if _winapi.WaitForSingleObject(process, 30000) != 0:
                    raise RuntimeError("The restarted test process did not finish closing")
            finally:
                _winapi.CloseHandle(process)
        if source and source.poll() is None:
            source.terminate()
            source.wait(timeout=20)
        uninstall = install / "unins000.exe"
        if uninstall.is_file():
            subprocess.run([str(uninstall), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True, timeout=90)
            wait_for(lambda: not uninstall.exists())


if __name__ == "__main__":
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ota-test-", dir=ROOT / "build") as scratch:
        root = Path(scratch)
        server, cert_file = local_https(root)
        with server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                exercise(root, server, cert_file)
            finally:
                server.shutdown()
