"""Small installer verifier; it contains the same public trust anchor as the app."""
import argparse
from pathlib import Path
import sys

from backend.licensing import (
    LicenseError, machine_code, read_license_file, trusted_public_key, verify_license,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["machine-code", "verify"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--license-file", type=Path)
    args = parser.parse_args(argv)
    status = 0
    try:
        code = machine_code()
        if args.command == "machine-code":
            result = code
        else:
            if args.license_file is None:
                raise LicenseError("未提供 CD-KEY 文件。")
            info = verify_license(read_license_file(args.license_file), code, trusted_public_key())
            result = f"授权验证通过：{info.summary}。"
    except LicenseError as exc:
        result, status = str(exc), 1
    try:
        args.output.write_text(result, encoding="utf-8")
    except OSError as exc:
        if sys.stderr is not None:
            print(f"Cannot write license verification result: {exc}", file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
