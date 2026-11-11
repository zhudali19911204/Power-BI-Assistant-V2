"""Build a licensed Windows release using only the publisher's public key."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.license_admin import default_key_directory

ROOT = Path(__file__).resolve().parents[1]


def find_iscc(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.is_file():
            raise ValueError(f"Inno Setup compiler not found: {explicit}")
        return explicit.resolve()
    discovered = shutil.which("ISCC.exe")
    if discovered:
        return Path(discovered)
    for variable, suffix in (
        ("LOCALAPPDATA", r"Programs\Inno Setup 6\ISCC.exe"),
        ("ProgramFiles(x86)", r"Inno Setup 6\ISCC.exe"),
        ("ProgramFiles", r"Inno Setup 6\ISCC.exe"),
    ):
        base = os.environ.get(variable)
        if base and (Path(base) / suffix).is_file():
            return Path(base) / suffix
    raise ValueError("Install Inno Setup 6, or pass --iscc with the path to ISCC.exe.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--version", default="2.0.2")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "win32" or struct.calcsize("P") != 8:
            raise ValueError("Use 64-bit Python on Windows to build this installer.")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", args.version):
            raise ValueError("Version must have three or four numeric components.")
        iscc = find_iscc(args.iscc)
        if importlib.util.find_spec("PyInstaller") is None:
            raise ValueError("Install build dependencies: python -m pip install -r requirements-build.txt")
        public_path = args.public_key or default_key_directory() / "public-key.pem"
        public_key = serialization.load_pem_public_key(public_path.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("The license public key must be an Ed25519 PEM public key.")
        output = args.output_dir.resolve()
        work = ROOT / "build"
        trust = work / "license-trust"
        trust.mkdir(parents=True, exist_ok=True)
        (trust / "_license_trust.py").write_text(
            f"PUBLIC_KEY = {public_key.public_bytes_raw()!r}\n", encoding="ascii")
        environment = dict(os.environ, PBI_BUILD_TRUST_DIR=str(trust))
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
             str(ROOT / "packaging" / "powerbi.spec")],
            cwd=ROOT, env=environment, check=True,
        )
        subprocess.run(
            [str(iscc), f"/DReleaseDir={output}", f"/DAppVersion={args.version}",
             str(ROOT / "packaging" / "installer.iss")],
            cwd=ROOT, check=True,
        )
        print(f"Installer: {output / f'PowerBIAssistant-Setup-{args.version}.exe'}")
        print("Distribute only the installer. Never distribute publisher keys or source code.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
