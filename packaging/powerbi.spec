# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

root = Path(SPECPATH).parent
trust_directory = os.environ["PBI_BUILD_TRUST_DIR"]
search_paths = [str(root), trust_directory]

app = Analysis(
    [str(root / "desktop.py")],
    pathex=search_paths,
    binaries=[],
    datas=[(str(root / "frontend"), "frontend")],
    hiddenimports=["_license_trust"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
app_exe = EXE(
    PYZ(app.pure), app.scripts, [],
    exclude_binaries=True,
    name="PowerBIAssistant",
    console=False,
    upx=False,
)
COLLECT(app_exe, app.binaries, app.datas, name="PowerBIAssistant", upx=False)

verifier = Analysis(
    [str(root / "scripts" / "license_helper.py")],
    pathex=search_paths,
    binaries=[],
    datas=[],
    hiddenimports=["_license_trust"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
EXE(
    PYZ(verifier.pure), verifier.scripts, verifier.binaries, verifier.datas, [],
    name="LicenseVerifier",
    console=False,
    upx=False,
)
