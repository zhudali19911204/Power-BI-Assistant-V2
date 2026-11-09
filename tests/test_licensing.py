import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend import licensing
from backend.app import create_app
from scripts import license_admin, license_helper

GUID = "cb647bca-6326-499b-96f1-a649c5a891a8"
OTHER_GUID = "f79584ea-5c0b-4590-9bb4-292eb8550fba"


@pytest.fixture
def authorization(monkeypatch):
    private = Ed25519PrivateKey.generate()
    code = licensing.machine_code_from_guid(GUID)
    key = license_admin.issue_license(private, code)
    monkeypatch.setitem(sys.modules, "_license_trust",
                        SimpleNamespace(PUBLIC_KEY=private.public_key().public_bytes_raw()))
    return private, code, key


def test_machine_identity_is_stable_and_does_not_expose_guid():
    code = licensing.machine_code_from_guid(GUID)
    assert code == licensing.machine_code_from_guid("  {" + GUID.upper() + "}  ")
    assert code != licensing.machine_code_from_guid(OTHER_GUID)
    assert GUID not in code
    assert len(licensing.decode_code(code, licensing.MACHINE_PREFIX, 16)) == 16


@pytest.mark.parametrize("guid", ["", "not-a-guid", "00000000-0000-0000-0000-000000000000"])
def test_bad_machine_identity_has_no_fallback(guid):
    with pytest.raises(licensing.LicenseError):
        licensing.machine_code_from_guid(guid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_unreadable_machine_identity_fails_closed(monkeypatch):
    import winreg

    def denied(*args):
        raise PermissionError("test denial")
    monkeypatch.setattr(winreg, "OpenKey", denied)
    with pytest.raises(licensing.LicenseError, match="无法读取"):
        licensing.machine_code()


def test_valid_permanent_license_and_pasted_format(authorization):
    private, code, key = authorization
    assert len(key) < 160
    licensing.verify_license(key, code, private.public_key())
    licensing.verify_license("\n " + key.lower().replace("-", " -\n") + "\t",
                             code, licensing.trusted_public_key())


def test_signed_license_for_another_machine_is_rejected(authorization):
    private, _, key = authorization
    with pytest.raises(licensing.LicenseError, match="不属于本机"):
        licensing.verify_license(key, licensing.machine_code_from_guid(OTHER_GUID), private.public_key())


def test_license_cannot_be_forged_with_another_issuer(authorization):
    _, code, key = authorization
    with pytest.raises(licensing.LicenseError, match="无效"):
        licensing.verify_license(key, code, Ed25519PrivateKey.generate().public_key())


@pytest.mark.parametrize("index", [0, 15, 16, 79])
def test_tampered_fingerprint_or_signature_is_rejected(authorization, index):
    private, code, key = authorization
    raw = bytearray(licensing.decode_code(key, licensing.LICENSE_PREFIX, 80))
    raw[index] ^= 1
    tampered = licensing.encode_code(licensing.LICENSE_PREFIX, bytes(raw))
    with pytest.raises(licensing.LicenseError, match="无效"):
        licensing.verify_license(tampered, code, private.public_key())


@pytest.mark.parametrize("key", ["", "wrong", "PBI2K-00000000", "PBI2K-AAAA", "x" * 2049, "PBI2K-汉字"])
def test_malformed_license_is_rejected(authorization, key):
    private, code, _ = authorization
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(key, code, private.public_key())


def test_signatures_are_product_and_permanence_scoped(authorization):
    private, code, _ = authorization
    fingerprint = licensing.decode_code(code, licensing.MACHINE_PREFIX, 16)
    key = licensing.encode_code(licensing.LICENSE_PREFIX, fingerprint + private.sign(fingerprint))
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(key, code, private.public_key())


def test_key_files_accept_installer_utf8_bom_and_bound_size(tmp_path, authorization):
    _, _, key = authorization
    path = tmp_path / "license.key"
    path.write_text(key + "\n", encoding="utf-8-sig")
    assert licensing.read_license_file(path) == key
    path.write_bytes(b"x" * (licensing.MAX_KEY_SIZE + 1))
    with pytest.raises(licensing.LicenseError, match="过大"):
        licensing.read_license_file(path)
    path.write_bytes(b"\xff")
    with pytest.raises(licensing.LicenseError, match="无法读取"):
        licensing.read_license_file(path)


@pytest.mark.parametrize("module", [None, SimpleNamespace(), SimpleNamespace(PUBLIC_KEY=b"bad"),
                                   SimpleNamespace(PUBLIC_KEY="not-bytes")])
def test_missing_or_invalid_embedded_trust_fails_closed(monkeypatch, module):
    monkeypatch.setitem(sys.modules, "_license_trust", module)
    with pytest.raises(licensing.LicenseError, match="公钥"):
        licensing.trusted_public_key()


def test_source_mode_remains_available_to_developer(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    app = create_app(tmp_path)
    assert app.title == "Power BI Assistant"


def test_frozen_backend_requires_license_before_creating_database(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PowerBIAssistant.exe"))
    monkeypatch.setenv("PBI_DATA_DIR", str(tmp_path / "alternate"))
    with pytest.raises(licensing.LicenseError, match="授权文件"):
        create_app(tmp_path / "alternate")
    assert not (tmp_path / "alternate").exists()


def test_frozen_backend_uses_executable_license_not_working_directory(monkeypatch, tmp_path, authorization):
    _, code, key = authorization
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "license.key").write_text(key, encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "PowerBIAssistant.exe"))
    monkeypatch.setattr(licensing, "machine_code", lambda: code)
    monkeypatch.chdir(tmp_path)
    assert create_app(tmp_path / "data").title == "Power BI Assistant"
    monkeypatch.setattr(licensing, "machine_code", lambda: licensing.machine_code_from_guid(OTHER_GUID))
    with pytest.raises(licensing.LicenseError, match="不属于本机"):
        create_app(tmp_path / "other")
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize("browser", [False, True])
def test_desktop_rejects_license_before_listening(monkeypatch, tmp_path, browser):
    import ctypes
    import desktop

    dialogs = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PowerBIAssistant.exe"))
    monkeypatch.setattr(sys, "argv", ["desktop"] + (["--browser", "--no-open"] if browser else []))
    monkeypatch.setenv("PBI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(MessageBoxW=lambda *args: dialogs.append(args))), raising=False)

    def unexpected_socket(*args, **kwargs):
        pytest.fail("Unauthorized app must not open a listening socket")
    monkeypatch.setattr(desktop.socket, "socket", unexpected_socket)
    assert desktop.main() == 1
    assert bool(dialogs) is not browser
    assert not (tmp_path / "data" / "assistant.sqlite3").exists()


def test_installer_helper_status_and_output(tmp_path, monkeypatch, authorization):
    _, code, key = authorization
    monkeypatch.setattr(license_helper, "machine_code", lambda: code)
    output = tmp_path / "result.txt"
    path = tmp_path / "license.key"
    assert license_helper.main(["machine-code", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == code
    path.write_text(key, encoding="utf-8-sig")
    args = ["verify", "--license-file", str(path), "--output", str(output)]
    assert license_helper.main(args) == 0
    path.write_text("invalid", encoding="utf-8")
    assert license_helper.main(args) == 1
    assert "格式" in output.read_text(encoding="utf-8")
    path.unlink()
    assert license_helper.main(args) == 1
    assert license_helper.main(["verify", "--output", str(output)]) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_publisher_dpapi_issuance_and_no_overwrite(tmp_path, capsys):
    directory = tmp_path / "publisher"
    assert license_admin.main(["--key-dir", str(directory), "init"]) == 0
    private = license_admin.load_private_key(directory)
    assert private.private_bytes_raw() not in (directory / "publisher-key.dpapi").read_bytes()
    public = serialization.load_pem_public_key((directory / "public-key.pem").read_bytes())
    assert public.public_bytes_raw() == private.public_key().public_bytes_raw()
    assert license_admin.main(["--key-dir", str(directory), "init"]) == 1
    path = tmp_path / "customer.txt"
    code = licensing.machine_code_from_guid(GUID)
    args = ["--key-dir", str(directory), "issue", "--machine-code", code, "--out", str(path)]
    assert license_admin.main(args) == 0
    licensing.verify_license(path.read_text(encoding="utf-8"), code, public)
    assert license_admin.main(args) == 1
    assert private.private_bytes_raw().hex() not in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_publisher_encrypted_backup_and_restore(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    backup = tmp_path / "publisher.backup.pem"
    monkeypatch.setattr(license_admin.getpass, "getpass", lambda _: "a-test-backup-password")
    assert license_admin.main(["--key-dir", str(source), "init"]) == 0
    assert license_admin.main(["--key-dir", str(source), "export", "--out", str(backup)]) == 0
    assert backup.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert license_admin.main(["--key-dir", str(target), "restore", "--backup", str(backup)]) == 0
    assert (source / "public-key.pem").read_bytes() == (target / "public-key.pem").read_bytes()
    monkeypatch.setattr(license_admin.getpass, "getpass", lambda _: "incorrect-backup-password")
    assert license_admin.main(["--key-dir", str(tmp_path / "bad"), "restore", "--backup", str(backup)]) == 1
    assert not (tmp_path / "bad").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_corrupt_publisher_key_cannot_be_decrypted():
    with pytest.raises(OSError):
        license_admin.protect_private_key(b"not-a-dpapi-blob", decrypt=True)
