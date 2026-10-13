from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
import zipfile
from pathlib import Path


def _verify_zip(package: Path, platform: str) -> None:
    with zipfile.ZipFile(package) as archive:
        bad_entry = archive.testzip()
        if bad_entry:
            raise ValueError(f"Corrupt ZIP entry: {bad_entry}")
        names = set(archive.namelist())

    if platform == "windows":
        expected = {"README.txt", "VirtualDomain.exe"}
        if names != expected:
            raise ValueError(f"Unexpected Windows package contents: {sorted(names)}")
    elif platform == "macos":
        app_executable = "VirtualDomain.app/Contents/MacOS/VirtualDomain"
        if "README.txt" not in names or not any(
            name.endswith(app_executable) for name in names
        ):
            raise ValueError("macOS package must contain README.txt and the app executable")
    else:
        raise ValueError(f"ZIP packaging is not configured for {platform}")


def _verify_tar_gz(package: Path) -> None:
    required = {"README.txt", "VirtualDomain/VirtualDomain"}
    with tarfile.open(package, mode="r:gz") as archive:
        members = archive.getmembers()
        names = {member.name.removeprefix("./") for member in members}
        missing = required - names
        if missing:
            raise ValueError(f"Linux package is missing entries: {sorted(missing)}")
        for member in members:
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"Cannot read archive entry: {member.name}")
                while stream.read(1024 * 1024):
                    pass


def verify_package(platform: str, package_base: Path) -> Path:
    platform = platform.lower()
    if platform == "windows":
        package = package_base.with_suffix(".zip")
        _verify_zip(package, platform)
    elif platform == "macos":
        package = package_base.with_suffix(".zip")
        _verify_zip(package, platform)
    elif platform == "linux":
        package = package_base.with_suffix(".tar.gz")
        _verify_tar_gz(package)
    else:
        raise ValueError(f"Unsupported package platform: {platform}")

    if not package.is_file() or package.stat().st_size == 0:
        raise ValueError(f"Package is missing or empty: {package}")

    with package.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checksum = package.with_name(package.name + ".sha256")
    checksum.write_text(f"{digest}  {package.name}\n", encoding="ascii")
    return checksum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=("windows", "linux", "macos"))
    parser.add_argument("--package", required=True, type=Path, help="Package path without its extension")
    args = parser.parse_args()
    try:
        checksum = verify_package(args.platform, args.package)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Package verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Package verified; SHA-256 written to {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
