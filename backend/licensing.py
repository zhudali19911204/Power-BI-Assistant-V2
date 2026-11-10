"""Offline permanent and issuance-based seven-day licenses for Windows."""
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import importlib
import logging
import math
from pathlib import Path
import re
import sys
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MACHINE_PREFIX = "PBI2M"
LICENSE_PREFIX = "PBI2K"
TRIAL_PREFIX = "PBI2T"
SIGNING_CONTEXT = b"Power BI Assistant V2\x00permanent-license-v1\x00"
TRIAL_SIGNING_CONTEXT = b"Power BI Assistant V2\x00trial-7-days-v1\x00"
TRIAL_SECONDS = 7 * 24 * 60 * 60
MAX_ISSUED_AT = 253402300799 - TRIAL_SECONDS
MAX_KEY_SIZE = 2048


class LicenseError(ValueError):
    """An authorization failure that can be shown to the user."""


@dataclass(frozen=True)
class LicenseInfo:
    issued_at: int | None = None

    def __post_init__(self) -> None:
        if self.issued_at is not None and (
                type(self.issued_at) is not int or not 0 < self.issued_at <= MAX_ISSUED_AT):
            raise LicenseError("试用授权的签发时间无效，请联系软件提供者。")

    @property
    def expires_at(self) -> int | None:
        return None if self.issued_at is None else self.issued_at + TRIAL_SECONDS

    @property
    def summary(self) -> str:
        expiry = self.expires_at
        if expiry is None:
            return "永久授权"
        stamp = datetime.fromtimestamp(expiry, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"7 天试用，到期时间：{stamp}（从签发时起算）"

    def check_time(self, now: float | None = None) -> None:
        expiry = self.expires_at
        if expiry is None:
            return
        current = time.time() if now is None else now
        if not math.isfinite(current) or (self.issued_at is not None and current < self.issued_at):
            raise LicenseError("系统时间早于试用 CD-KEY 的签发时间，请校准系统时间后重试。")
        if current >= expiry:
            raise LicenseError(f"试用授权已到期：{self.summary}。请联系软件提供者取得永久 CD-KEY，重新运行安装程序激活。")


class LicenseGuard:
    """Keep an open application from extending its trial by rolling back the clock."""

    def __init__(self, info: LicenseInfo):
        self.info = info
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.failure: str | None = None

    def check(self) -> None:
        if self.info.expires_at is None:
            return
        if self.failure:
            raise LicenseError(self.failure)
        current = max(time.time(), self.started_at + time.monotonic() - self.started_monotonic)
        try:
            self.info.check_time(current)
        except LicenseError as exc:
            self.failure = str(exc)
            logging.getLogger(__name__).warning("Runtime license rejected: %s", exc)
            raise


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


def verify_license(cd_key: str, current_machine: str, public_key: Ed25519PublicKey,
                   *, now: float | None = None) -> LicenseInfo:
    if len(cd_key) > MAX_KEY_SIZE:
        raise LicenseError("授权码过长，请完整复制正确的授权码。")
    trial = re.sub(r"[\s-]", "", cd_key).upper().startswith(TRIAL_PREFIX)
    raw = decode_code(cd_key, TRIAL_PREFIX if trial else LICENSE_PREFIX, 88 if trial else 80)
    payload, signature = raw[:-64], raw[-64:]
    fingerprint = payload[:16]
    try:
        public_key.verify(signature, (TRIAL_SIGNING_CONTEXT if trial else SIGNING_CONTEXT) + payload)
    except InvalidSignature as exc:
        raise LicenseError("CD-KEY 无效或已被修改，请联系软件提供者。") from exc
    if not hmac.compare_digest(fingerprint, decode_code(current_machine, MACHINE_PREFIX, 16)):
        raise LicenseError("此 CD-KEY 不属于本机，请将本机机器码发给软件提供者重新授权。")
    info = LicenseInfo(int.from_bytes(payload[16:], "big") if trial else None)
    info.check_time(now)
    return info


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


def require_runtime_license() -> LicenseInfo | None:
    if not getattr(sys, "frozen", False):
        return
    path = Path(sys.executable).resolve().with_name("license.key")
    return verify_license(read_license_file(path), machine_code(), trusted_public_key())
