"""
On-demand AMO / TOM (Tabular Object Model) assemblies.

Writing a measure into the open Desktop model needs TOM (`Microsoft.AnalysisServices.Tabular.dll`),
which Power BI Desktop does NOT ship in its bin. Rather than make the user install anything, we download
the official NuGet package once, extract the four .NET-Framework DLLs, and cache them under the user's home
dir; pythonnet loads them from there (same trick as the bundled ADOMD client). The download goes through
the OS trust store so it works behind a corporate TLS-inspecting proxy (see net.py).
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

from ..net import enable_os_trust_store

# Pinned, known-good version of the .NET Framework AMO/TOM package.
_AMO_VERSION = "19.84.1"
_NUPKG_URL = (
    "https://api.nuget.org/v3-flatcontainer/microsoft.analysisservices.retail.amd64/"
    f"{_AMO_VERSION}/microsoft.analysisservices.retail.amd64.{_AMO_VERSION}.nupkg"
)
# Load order matters (Core before the rest); the last one carries the TOM types we use.
AMO_DLLS = [
    "Microsoft.AnalysisServices.Core.dll",
    "Microsoft.AnalysisServices.dll",
    "Microsoft.AnalysisServices.Tabular.Json.dll",
    "Microsoft.AnalysisServices.Tabular.dll",
]
CACHE_DIR = Path(os.getenv("PBI_DATA_DIR", str(Path(__file__).resolve().parents[3] / "data"))) / "lib" / "amo"

_NET45 = re.compile(r"^lib/net45/[^/]+\.dll$")


def _cached() -> bool:
    return all((CACHE_DIR / d).exists() for d in AMO_DLLS)


def ensure_amo_dlls() -> Path:
    """Return the directory holding the TOM DLLs, downloading+extracting them on first use."""
    if _cached():
        return CACHE_DIR
    enable_os_trust_store()
    import urllib.request

    with urllib.request.urlopen(_NUPKG_URL, timeout=180) as resp:  # noqa: S310 — pinned HTTPS NuGet URL
        data = resp.read()
    archive = zipfile.ZipFile(io.BytesIO(data))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set(AMO_DLLS)
    for entry in archive.namelist():
        base = entry.rsplit("/", 1)[-1]
        if base in wanted and _NET45.match(entry):
            (CACHE_DIR / base).write_bytes(archive.read(entry))
    missing = [d for d in AMO_DLLS if not (CACHE_DIR / d).exists()]
    if missing:
        raise RuntimeError(f"AMO 下载不完整，缺少：{missing}")
    return CACHE_DIR
