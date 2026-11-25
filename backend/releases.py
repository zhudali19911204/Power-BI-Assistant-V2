"""Windows release metadata shared by the desktop client and publisher tooling."""
import base64
from dataclasses import asdict, dataclass
import json
import re
import sys
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DEFAULT_VERSION = "2.0.13"
RELEASES_URL = "https://github.com/zhudali19911204/Power-BI-Assistant-V2/releases"
DEFAULT_FEED = RELEASES_URL + "/latest/download/latest.json"
if getattr(sys, "frozen", False):
    from _license_trust import APP_VERSION, UPDATE_FEED
else:
    APP_VERSION, UPDATE_FEED = DEFAULT_VERSION, DEFAULT_FEED

# Domain separation keeps release signatures distinct from permanent and trial licenses.
SIGNING_CONTEXT = b"Power BI Assistant V2\x00windows-x64-update-v1\x00"
MANIFEST_LIMIT = 64 * 1024


class UpdateError(ValueError):
    pass


def version_number(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){2,3}", value):
        raise UpdateError("版本号须由三段或四段数字组成。")
    numbers = tuple(map(int, value.split(".")))
    if any(n > 65535 for n in numbers):
        raise UpdateError("版本号的每一段须小于 65536。")
    return numbers + (0,) * (4 - len(numbers))


def require_https(url: str) -> None:
    if isinstance(url, str) and 0 < len(url) <= 8192:
        try:
            parts = urlsplit(url)
            if (parts.scheme == "https" and parts.hostname and parts.username is None
                    and parts.password is None and parts.port != 0 and not parts.fragment and "\\" not in url
                    and not any(ord(c) <= 32 or ord(c) == 127 for c in url)):
                return
        except ValueError:
            pass
    raise UpdateError("更新地址必须是有效的 HTTPS 地址。")


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: str
    notes: str

    def __post_init__(self):
        version_number(self.version)
        require_https(self.url)
        if type(self.size) is not int or not 0 < self.size <= 1024**3:
            raise UpdateError("更新清单中的安装包大小无效。")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise UpdateError("更新清单中的文件校验值无效。")
        if not isinstance(self.notes, str) or len(self.notes) > 6000:
            raise UpdateError("更新说明格式无效或超过 6000 字。")

    def signing_bytes(self) -> bytes:
        return SIGNING_CONTEXT + json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("ascii")

    def summary(self):
        return {"version": self.version, "notes": self.notes, "size": self.size}


def verified_release(data: bytes, key: Ed25519PublicKey) -> Release:
    if len(data) > MANIFEST_LIMIT:
        raise UpdateError("更新清单过大。")
    try:
        envelope = json.loads(data)
        signature = base64.b64decode(envelope["signature"], validate=True)
        release = Release(**envelope["release"])
        key.verify(signature, release.signing_bytes())
        return release
    except InvalidSignature as exc:
        raise UpdateError("更新清单的发布者签名无效。") from exc
    except UpdateError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise UpdateError("无法读取更新清单。") from exc
