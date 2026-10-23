"""Fetch pinned native development tools from their upstream distribution sites."""

import concurrent.futures
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / "runtime" / "downloads"
TOOLS = ROOT / "runtime" / "tools"
SOURCES = {
    "postgresql": (
        "17.11-5",
        "https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64-binaries.zip",
        "80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f",
    ),
    "napcat": (
        "4.18.41",
        "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.41/"
        "NapCat.Shell.Windows.Node.zip",
        "51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0",
    ),
    "pgvector": (
        "0.8.7",
        "https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.zip",
        "7b3b8188a5cc0aa96db3c8f3fca1e78a8a612c46545065490c76448705c6b29e",
    ),
}


def install(name: str) -> None:
    version, url, expected_hash = SOURCES[name]
    target = TOOLS / name
    marker = target / "lisa-tool.json"
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8"))["version"] != version:
            raise RuntimeError(f"{name}: existing version differs; upgrade explicitly")
        print(f"{name} {version}: already extracted", flush=True)
        return
    archive = DOWNLOADS / f"{name}-{version}.zip"
    if not archive.exists():
        print(f"Downloading {name} {version}", flush=True)
        partial = archive.with_suffix(".part")
        request = urllib.request.Request(url, headers={"User-Agent": "LISA-dev-setup"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(archive)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if expected_hash and digest != expected_hash:
        raise RuntimeError(f"{name}: checksum mismatch; remove the archive and retry")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            # EDB's zip also ships GUI tooling; only extract the server/development SDK.
            path = member.filename
            if name == "postgresql":
                if not path.startswith(
                    ("pgsql/bin/", "pgsql/lib/", "pgsql/include/", "pgsql/share/")
                ):
                    continue
                path = path.removeprefix("pgsql/")
            elif name == "pgvector":
                path = path.partition("/")[2]
            if not path or member.is_dir():
                continue
            dest = (target / path).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise RuntimeError(f"Unsafe archive member: {path}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
    marker.write_text(
        json.dumps({"version": version, "url": url, "sha256": digest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{name} {version}: extracted; sha256={digest}", flush=True)


if __name__ == "__main__":
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(install, SOURCES))
