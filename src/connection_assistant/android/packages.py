"""Download and safely unpack Android packages used by the onboarding flow."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TINDER_PACKAGE = "com.tinder"
TINDER_XAPK_URL = "https://d.apkpure.com/b/XAPK/com.tinder?version=latest"
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1536 * 1024 * 1024
MAX_PACKAGE_FILES = 128

ProgressFn = Callable[[str], None]


class AndroidPackageError(RuntimeError):
    """An Android package could not be downloaded, validated, or unpacked."""


@dataclass(frozen=True)
class ExtractedXapk:
    apk_paths: list[Path]
    obb_paths: list[Path]


def download_latest_tinder(*, on_progress: ProgressFn | None = None) -> Path:
    """Download APKPure's latest Tinder XAPK to a private temporary file."""
    emit = on_progress or (lambda _message: None)
    emit("downloading the latest Tinder XAPK from APKPure")
    request = urllib.request.Request(
        TINDER_XAPK_URL,
        headers={
            "Accept": "application/octet-stream,application/zip,*/*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/139.0 Safari/537.36"
            ),
        },
    )
    fd, raw_path = tempfile.mkstemp(prefix="icebreaker-tinder-", suffix=".xapk")
    os.close(fd)
    path = Path(raw_path)
    completed = False
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed URL
            final_url = urllib.parse.urlsplit(response.geturl())
            if final_url.scheme.lower() != "https":
                raise AndroidPackageError("APKPure redirected the download to an unsafe URL")
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise AndroidPackageError("the Tinder download is unexpectedly large")
            downloaded = 0
            next_report = 25 * 1024 * 1024
            with path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise AndroidPackageError("the Tinder download is unexpectedly large")
                    output.write(chunk)
                    if downloaded >= next_report:
                        emit(f"downloaded {downloaded // (1024 * 1024)} MB")
                        next_report += 25 * 1024 * 1024
        validate_xapk(path)
        completed = True
        emit("Tinder XAPK downloaded and verified")
        return path
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            message = (
                "APKPure blocked the automatic download. The download page will open in your "
                "browser; when it finishes, choose the XAPK in Icebreaker Connect."
            )
        else:
            message = f"APKPure download failed (HTTP {exc.code})"
        raise AndroidPackageError(message) from exc
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise AndroidPackageError("the Tinder XAPK download was invalid or incomplete") from exc
    finally:
        if not completed:
            _safe_unlink(path)


def validate_xapk(path: str | Path) -> None:
    """Validate that an XAPK is a bounded archive for Tinder with installable APKs."""
    with zipfile.ZipFile(path) as archive:
        _validated_entries(archive)


def extract_xapk(path: str | Path, destination: str | Path) -> ExtractedXapk:
    """Extract only validated APK/OBB payloads, never arbitrary archive paths."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        apk_entries, obb_entries = _validated_entries(archive)
        apk_paths = [
            _copy_entry(archive, info, destination / f"apk-{index:03d}.apk")
            for index, info in enumerate(apk_entries)
        ]
        obb_paths = [
            _copy_entry(archive, info, destination / Path(info.filename).name)
            for info in obb_entries
        ]
    return ExtractedXapk(apk_paths=apk_paths, obb_paths=obb_paths)


def _validated_entries(
    archive: zipfile.ZipFile,
) -> tuple[list[zipfile.ZipInfo], list[zipfile.ZipInfo]]:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > MAX_PACKAGE_FILES:
        raise AndroidPackageError("the XAPK contains too many files")
    if any(info.flag_bits & 0x1 for info in infos):
        raise AndroidPackageError("encrypted XAPK files are not supported")

    manifests = [info for info in infos if PurePosixPath(info.filename).name == "manifest.json"]
    if len(manifests) != 1 or manifests[0].file_size > 1024 * 1024:
        raise AndroidPackageError("the XAPK manifest is missing or invalid")
    try:
        manifest = json.loads(archive.read(manifests[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AndroidPackageError("the XAPK manifest is invalid") from exc
    if manifest.get("package_name") != TINDER_PACKAGE:
        raise AndroidPackageError("the selected XAPK is not Tinder")

    apk_entries = [info for info in infos if info.filename.lower().endswith(".apk")]
    obb_entries = [
        info for info in infos if _is_tinder_obb_path(PurePosixPath(info.filename))
    ]
    if not apk_entries:
        raise AndroidPackageError("the XAPK does not contain an APK")
    total_size = sum(info.file_size for info in [*apk_entries, *obb_entries])
    if total_size > MAX_EXTRACTED_BYTES:
        raise AndroidPackageError("the XAPK expands beyond the safe size limit")
    obb_names = [PurePosixPath(info.filename).name.lower() for info in obb_entries]
    if len(obb_names) != len(set(obb_names)):
        raise AndroidPackageError("the XAPK contains duplicate expansion files")
    apk_entries.sort(key=lambda info: _apk_sort_key(PurePosixPath(info.filename).name))
    return apk_entries, obb_entries


def _apk_sort_key(name: str) -> tuple[int, str]:
    lowered = name.lower()
    is_base = lowered in {"base.apk", "com.tinder.apk"} or not lowered.startswith(
        ("config.", "split_")
    )
    return (0 if is_base else 1, lowered)


def _is_tinder_obb_path(path: PurePosixPath) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return (
        len(parts) >= 4
        and parts[-1].endswith(".obb")
        and parts[-3:-1] == ("obb", TINDER_PACKAGE)
    )


def _copy_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> Path:
    with archive.open(info) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    return target


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
