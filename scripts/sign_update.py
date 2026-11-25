"""Sign latest.json for a finished Power BI Assistant installer on the publisher's machine."""
import argparse
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from backend.releases import RELEASES_URL, Release, verified_release
from scripts.license_admin import default_key_directory, load_private_key


def sign_installer(installer, private_key, *, url=None, notes=""):
    match = re.fullmatch(r"PowerBIAssistant-Setup-([0-9.]+)\.exe", installer.name)
    if not match:
        raise ValueError("请选择构建生成的 PowerBIAssistant-Setup-<版本>.exe。")
    version = match[1]
    with installer.open("rb") as package:
        release = Release(version, url or f"{RELEASES_URL}/download/v{version}/{installer.name}",
                          os.fstat(package.fileno()).st_size,
                          hashlib.file_digest(package, "sha256").hexdigest(), notes)
    signature = private_key.sign(release.signing_bytes())
    envelope = {"release": asdict(release), "signature": base64.b64encode(signature).decode("ascii")}
    data = (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    verified_release(data, private_key.public_key())
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--url", help="版本专用的 HTTPS 安装包地址，默认使用 GitHub 标签 v<版本>")
    parser.add_argument("--notes", default="", help="在客户端显示的纯文本更新说明")
    parser.add_argument("--key-dir", type=Path, help="与客户端内置公钥对应的发布者密钥目录")
    parser.add_argument("--output", type=Path, help="默认写入安装包旁的 latest.json")
    args = parser.parse_args(argv)
    try:
        output = args.output or args.installer.with_name("latest.json")
        if output.suffix.lower() != ".json":
            raise ValueError("版本清单的输出文件须为 .json。")
        private = load_private_key(args.key_dir or default_key_directory())
        data = sign_installer(args.installer, private, url=args.url, notes=args.notes)
        handle = tempfile.NamedTemporaryFile(dir=output.parent, delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(data)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Signed manifest: {output}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Signing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
