"""Isolated ZIP worker, executed by the plugin's current Python interpreter."""

import json
import os
import sys
import zipfile
from pathlib import Path


def validated_files(manifest: dict) -> list[tuple[Path, str]]:
    root = Path(manifest["root"]).resolve()
    total = 0
    seen, files = set(), []
    for item, expected in manifest["files"]:
        path = Path(item)
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError("Unsafe archive input")
        cursor = path.absolute()
        while cursor != root and cursor != cursor.parent:
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise ValueError("Linked archive input")
            cursor = cursor.parent
        relative = resolved.relative_to(root).as_posix()
        if relative.casefold() in seen or not resolved.is_file():
            raise ValueError("Duplicate or missing file")
        seen.add(relative.casefold())
        if resolved.stat().st_size != expected:
            raise ValueError("Incomplete download")
        total += expected
        if total > manifest["max_bytes"]:
            raise ValueError("Download exceeds configured limit")
        files.append((resolved, relative))
    if not files:
        raise ValueError("No files")
    return files


def verify_plain_zip(manifest: dict, files: list[tuple[Path, str]]):
    output = Path(manifest["output"])
    partial = output.with_suffix(".partial")
    if partial.stat().st_size > manifest["upload_bytes"]:
        raise ValueError("Archive exceeds upload limit")
    expected = {name: size for (_, name), (_, size) in zip(files, manifest["files"], strict=True)}
    with zipfile.ZipFile(partial) as archive:
        entries = archive.infolist()
        if (
            len(entries) != len(expected)
            or {entry.filename: entry.file_size for entry in entries} != expected
            or any(entry.flag_bits & 1 for entry in entries)
            or archive.testzip() is not None
        ):
            raise ValueError("ZIP verification failed")
    os.replace(partial, output)


def build(manifest: dict):
    files = validated_files(manifest)
    output = Path(manifest["output"])
    partial = output.with_suffix(".partial")
    try:
        with zipfile.ZipFile(
            partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
        ) as archive:
            for resolved, relative in files:
                archive.write(resolved, relative)
                if partial.stat().st_size > manifest["upload_bytes"]:
                    raise ValueError("Archive exceeds upload limit")
        verify_plain_zip(manifest, files)
    finally:
        partial.unlink(missing_ok=True)


def split_archive(source: Path, part_size: int):
    """Write byte volumes compatible with 7-Zip's .001 reader."""
    if part_size < 1:
        raise ValueError("Volume size must be positive")
    remaining = source.stat().st_size
    with source.open("rb") as stream:
        index = 1
        while remaining:
            size = min(part_size, remaining)
            path = source.with_name(f"{source.name}.split.{index:03d}")
            with path.open("wb") as part:
                left = size
                while left:
                    chunk = stream.read(min(left, 4 * 1024 * 1024))
                    if not chunk:
                        raise OSError("Archive changed during split")
                    part.write(chunk)
                    left -= len(chunk)
            remaining -= size
            index += 1


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "split":
            split_archive(Path(sys.argv[2]), int(sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "verify":
            manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            verify_plain_zip(manifest, validated_files(manifest))
        else:
            build(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    except Exception as exc:
        # Controlled diagnostics only; do not echo filenames from downloaded material.
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
