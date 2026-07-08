from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

import nltk


DEFAULT_DOWNLOAD_DIR = "/usr/local/share/nltk_data"
RESOURCES: tuple[tuple[str, str, str], ...] = (
    ("tokenizers", "punkt_tab", "tokenizers/punkt_tab/english/"),
    (
        "taggers",
        "averaged_perceptron_tagger_eng",
        "taggers/averaged_perceptron_tagger_eng/",
    ),
)


def _download_dir() -> str:
    return os.environ.get("NLTK_DATA") or DEFAULT_DOWNLOAD_DIR


def _mirror_prefix() -> str:
    return (os.environ.get("GITHUB_MIRROR_PREFIX") or "").strip().rstrip("/")


def _resource_urls(category: str, package_name: str) -> list[str]:
    urls: list[str] = []
    mirror_prefix = _mirror_prefix()
    if mirror_prefix:
        urls.append(
            f"{mirror_prefix}/nltk/nltk_data/raw/gh-pages/packages/{category}/{package_name}.zip"
        )
    urls.append(
        f"https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{category}/{package_name}.zip"
    )
    return urls


def _add_download_dir_to_nltk_path(download_dir: str) -> str:
    if download_dir not in nltk.data.path:
        nltk.data.path.insert(0, download_dir)
    return os.path.realpath(download_dir)


def _present(download_dir_real: str, lookup_path: str) -> bool:
    try:
        found = nltk.data.find(lookup_path)
    except LookupError:
        return False

    found_path = os.path.realpath(str(getattr(found, "path", found)))
    return found_path == download_dir_real or found_path.startswith(
        download_dir_real + os.sep
    )


def _normalize_existing_layout(download_dir: str, category: str, package_name: str) -> None:
    category_root = os.path.join(download_dir, category)
    target_root = os.path.join(category_root, package_name)
    flat_root = os.path.join(download_dir, package_name)
    nested_roots = (
        os.path.join(download_dir, "nltk_data", category, package_name),
        os.path.join(download_dir, "nltk_data", package_name),
    )

    os.makedirs(category_root, exist_ok=True)

    if os.path.isdir(flat_root) and not os.path.isdir(target_root):
        os.replace(flat_root, target_root)

    for nested_root in nested_roots:
        if os.path.isdir(nested_root) and not os.path.isdir(target_root):
            os.replace(nested_root, target_root)

    nested_root_parent = os.path.join(download_dir, "nltk_data")
    if os.path.isdir(nested_root_parent):
        try:
            shutil.rmtree(nested_root_parent)
        except OSError:
            # Keep startup resilient if some files are still in use.
            pass


def _safe_destination(download_dir_real: str, relative_path: str) -> Path:
    destination = Path(download_dir_real, relative_path)
    destination_real = os.path.realpath(destination)
    if destination_real != download_dir_real and not destination_real.startswith(
        download_dir_real + os.sep
    ):
        raise RuntimeError(f"Refusing to extract outside NLTK_DATA: {relative_path}")
    return Path(destination_real)


def _archive_relative_path(
    member_name: str, category: str, package_name: str
) -> str | None:
    normalized = member_name.replace("\\", "/").lstrip("/")
    if not normalized or normalized.startswith("__MACOSX/"):
        return None

    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts:
        return None

    if parts[0] == "nltk_data":
        parts = parts[1:]
    if not parts:
        return None

    if parts[0] == category:
        relative_parts = parts
    elif parts[0] == package_name:
        relative_parts = [category, *parts]
    else:
        relative_parts = [category, package_name, *parts]

    return "/".join(relative_parts)


def _extract_archive(
    archive_bytes: bytes, download_dir: str, category: str, package_name: str
) -> None:
    download_dir_real = os.path.realpath(download_dir)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for member in archive.infolist():
            relative_path = _archive_relative_path(member.filename, category, package_name)
            if relative_path is None:
                continue

            destination = _safe_destination(download_dir_real, relative_path)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _download_archive(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def ensure_nltk_resources(*, allow_build_skip: bool = False) -> bool:
    if allow_build_skip and (os.environ.get("SKIP_NLTK_PRELOAD") or "").lower() == "true":
        print("Skipping NLTK preload during image build")
        return False

    download_dir = _download_dir()
    os.makedirs(download_dir, exist_ok=True)
    download_dir_real = _add_download_dir_to_nltk_path(download_dir)

    missing: list[tuple[str, str, str]] = []
    for category, package_name, lookup_path in RESOURCES:
        _normalize_existing_layout(download_dir, category, package_name)
        if not _present(download_dir_real, lookup_path):
            missing.append((category, package_name, lookup_path))

    if not missing:
        print("NLTK resources already present.")
        return True

    print(
        "Downloading missing NLTK resources to "
        f"{download_dir}: {', '.join(package_name for _, package_name, _ in missing)}"
    )

    for category, package_name, lookup_path in missing:
        last_error: Exception | None = None
        shutil.rmtree(os.path.join(download_dir, category, package_name), ignore_errors=True)
        shutil.rmtree(os.path.join(download_dir, package_name), ignore_errors=True)

        for url in _resource_urls(category, package_name):
            try:
                print(f"Fetching {category}/{package_name} from {url}")
                _extract_archive(_download_archive(url), download_dir, category, package_name)
                _normalize_existing_layout(download_dir, category, package_name)
                if _present(download_dir_real, lookup_path):
                    last_error = None
                    break
                last_error = RuntimeError(
                    f"Package extracted but not discoverable: {category}/{package_name}"
                )
            except Exception as exc:  # pragma: no cover - network/runtime path
                print(f"Download failed from {url}: {exc}")
                last_error = exc

        if last_error is not None:
            raise RuntimeError(
                f"Failed to install NLTK resource: {category}/{package_name}: {last_error}"
            ) from last_error

    for category, package_name, lookup_path in RESOURCES:
        if not _present(download_dir_real, lookup_path):
            raise RuntimeError(
                f"NLTK preload verification failed: {category}/{package_name} is still missing"
            )

    print("NLTK resources ready.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure required NLTK resources exist.")
    parser.add_argument(
        "--build-preload",
        action="store_true",
        help="Honor SKIP_NLTK_PRELOAD=true and exit successfully when set.",
    )
    args = parser.parse_args(argv)
    ensure_nltk_resources(allow_build_skip=args.build_preload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
