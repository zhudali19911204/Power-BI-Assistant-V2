"""Publisher-only key management. Never include this module in a release."""
import argparse
import ctypes
from ctypes import wintypes
import getpass
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.licensing import (
    LICENSE_PREFIX, MACHINE_PREFIX, SIGNING_CONTEXT, LicenseError, decode_code, encode_code,
)


def default_key_directory() -> Path:
    if sys.platform != "win32" or not os.environ.get("LOCALAPPDATA"):
        raise LicenseError("发码工具需要 Windows 用户配置目录。")
    return Path(os.environ["LOCALAPPDATA"]) / "PowerBIAssistantPublisher" / "licensing"


def protect_private_key(data: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise LicenseError("签发私钥使用 Windows DPAPI 保护，只能在 Windows 上操作。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


def save_key_pair(directory: Path, key: Ed25519PrivateKey) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    private_path, public_path = directory / "publisher-key.dpapi", directory / "public-key.pem"
    if private_path.exists() or public_path.exists():
        raise LicenseError("此目录已有签发密钥，拒绝覆盖。请继续使用原密钥，或选择新的空目录。")
    protected = protect_private_key(key.private_bytes_raw())
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with private_path.open("xb") as handle:
        handle.write(protected)
    with public_path.open("xb") as handle:
        handle.write(public)


def load_private_key(directory: Path) -> Ed25519PrivateKey:
    protected = (directory / "publisher-key.dpapi").read_bytes()
    return Ed25519PrivateKey.from_private_bytes(protect_private_key(protected, decrypt=True))


def issue_license(key: Ed25519PrivateKey, code: str) -> str:
    fingerprint = decode_code(code, MACHINE_PREFIX, 16)
    return encode_code(LICENSE_PREFIX, fingerprint + key.sign(SIGNING_CONTEXT + fingerprint))


def backup_password(*, confirm: bool) -> bytes:
    password = getpass.getpass("Backup password (at least 12 characters): ")
    if len(password) < 12:
        raise LicenseError("备份密码至少需要 12 个字符。")
    if confirm and password != getpass.getpass("Confirm backup password: "):
        raise LicenseError("两次输入的备份密码不一致。")
    return password.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Power BI Assistant 离线授权签发工具（仅发布者使用）")
    parser.add_argument("--key-dir", type=Path, help="密钥目录，默认保存在当前 Windows 用户的本地应用数据中")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="生成签发密钥，私钥由当前 Windows 用户 DPAPI 加密")
    issue = commands.add_parser("issue", help="为指定机器码签发永久 CD-KEY")
    issue.add_argument("--machine-code", required=True)
    issue.add_argument("--out", type=Path, help="另存为 UTF-8 文件；不覆盖现有文件")
    export = commands.add_parser("export", help="使用密码加密导出私钥备份，请勿发送给客户")
    export.add_argument("--out", type=Path, required=True)
    restore = commands.add_parser("restore", help="从密码加密备份恢复到空密钥目录")
    restore.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        directory = args.key_dir or default_key_directory()
        if args.command == "init":
            save_key_pair(directory, Ed25519PrivateKey.generate())
            print(f"签发密钥已保存至：{directory}\n请使用 export 命令创建密码加密备份。")
        elif args.command == "issue":
            cd_key = issue_license(load_private_key(directory), args.machine_code)
            if args.out:
                with args.out.open("x", encoding="utf-8") as handle:
                    handle.write(cd_key + "\n")
            print(cd_key)
        elif args.command == "export":
            data = load_private_key(directory).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(backup_password(confirm=True)))
            with args.out.open("xb") as handle:
                handle.write(data)
            print(f"加密备份已保存至：{args.out}")
        elif args.command == "restore":
            data = args.backup.read_bytes()
            if not data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----"):
                raise LicenseError("只接受密码加密的 PKCS8 私钥备份。")
            key = serialization.load_pem_private_key(data, password=backup_password(confirm=False))
            if not isinstance(key, Ed25519PrivateKey):
                raise LicenseError("备份不是 Ed25519 签发私钥。")
            save_key_pair(directory, key)
            print(f"签发密钥已恢复至：{directory}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"授权操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
