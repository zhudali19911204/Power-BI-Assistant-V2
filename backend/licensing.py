"""Offline, permanent Ed25519 licenses bound to a Windows installation."""
import base64
import binascii
import hashlib
import hmac
import importlib
from pathlib import Path
import re
import sys
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MACHINE_PREFIX = "PBI2M"
LICENSE_PREFIX = "PBI2K"
SIGNING_CONTEXT = b"Power BI Assistant V2\x00permanent-license-v1\x00"
MAX_KEY_SIZE = 2048


class LicenseError(ValueError):
    """An authorization failure that can be shown to the user."""


def encode_code(prefix: str, data: bytes) -> str:
    text = base64.b32encode(data).decode("ascii").rstrip("=")
    return prefix + "-" + "-".join(text[i:i + 8] for i in range(0, len(text), 8))


def decode_code(text: str, prefix: str, size: int) -> bytes:
    if len(text) > MAX_KEY_SIZE:
        raise LicenseError("授权码过长，请完整复制正确的授权码。")
    compact = re.sub(r"[\s-]", "", text).upper()
    if not compact.startswith(prefix):
        raise LicenseError("授权码格式不正确，请勿混淆机器码和 CD-KEY。")
    encoded = compact[len(prefix):]
    try:
        raw = base64.b32decode(encoded + "=" * (-len(encoded) % 8))
    except (binascii.Error, ValueError) as exc:
        raise LicenseError("授权码格式不正确，请重新复制。") from exc
    if len(raw) != size or base64.b32encode(raw).decode("ascii").rstrip("=") != encoded:
        raise LicenseError("授权码长度或编码不正确，请重新复制。")
    return raw


def machine_code_from_guid(guid: str) -> str:
    try:
        identity = uuid.UUID(guid.strip())
    except (ValueError, AttributeError) as exc:
        raise LicenseError("Windows 机器标识无效，无法生成机器码。") from exc
    if not identity.int:
        raise LicenseError("Windows 机器标识为空，无法生成机器码。")
    digest = hashlib.sha256(SIGNING_CONTEXT + identity.bytes).digest()[:16]
    return encode_code(MACHINE_PREFIX, digest)


def machine_code() -> str:
    if sys.platform != "win32":
        raise LicenseError("授权安装版仅支持 Windows。")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid, kind = winreg.QueryValueEx(key, "MachineGuid")
    except OSError as exc:
        raise LicenseError("无法读取 Windows 机器标识，请检查注册表读取权限。") from exc
    if kind != winreg.REG_SZ or not isinstance(guid, str):
        raise LicenseError("Windows 机器标识格式异常，无法验证授权。")
    return machine_code_from_guid(guid)


def verify_license(cd_key: str, current_machine: str, public_key: Ed25519PublicKey) -> None:
    raw = decode_code(cd_key, LICENSE_PREFIX, 80)
    fingerprint, signature = raw[:16], raw[16:]
    try:
        public_key.verify(signature, SIGNING_CONTEXT + fingerprint)
    except InvalidSignature as exc:
        raise LicenseError("CD-KEY 无效或已被修改，请联系软件提供者。") from exc
    if not hmac.compare_digest(fingerprint, decode_code(current_machine, MACHINE_PREFIX, 16)):
        raise LicenseError("此 CD-KEY 不属于本机，请将本机机器码发给软件提供者重新授权。")


def trusted_public_key() -> Ed25519PublicKey:
    try:
        # This module is generated from the publisher's public key and embedded in the EXEs.
        module = importlib.import_module("_license_trust")
        key = module.PUBLIC_KEY
        if not isinstance(key, bytes):
            raise ValueError("Invalid public key type")
        return Ed25519PublicKey.from_public_bytes(key)
    except (ImportError, AttributeError, ValueError) as exc:
        raise LicenseError("安装包缺少有效的授权验证公钥，请重新获取正式安装包。") from exc


def read_license_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_KEY_SIZE + 1)
        if len(content) > MAX_KEY_SIZE:
            raise LicenseError("授权文件过大，请重新运行安装程序输入 CD-KEY。")
        return content.decode("utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise LicenseError("无法读取授权文件，请重新运行安装程序输入 CD-KEY。") from exc


def require_runtime_license() -> None:
    if not getattr(sys, "frozen", False):
        return
    path = Path(sys.executable).resolve().with_name("license.key")
    verify_license(read_license_file(path), machine_code(), trusted_public_key())
