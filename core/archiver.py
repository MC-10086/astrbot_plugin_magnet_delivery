"""Cancellable direct-child compression; never start an associated GUI application."""

import asyncio
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

from .archive_worker import validated_files
from .delivery import DeliveryError, engine

WINDOWS_ZIP_COMMAND = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$job = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($env:ASTRBOT_MAGNET_ZIP_JOB))
$path = [IO.Path]::ChangeExtension($job.output, '.partial')
$archive = [IO.Compression.ZipFile]::Open($path, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($entry in $job.entries) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $entry.source, $entry.name, [IO.Compression.CompressionLevel]::Fastest
        ) | Out-Null
    }
} finally {
    $archive.Dispose()
}
"""


async def run_program(args: list[str], cwd: Path, partial: Path, limit: int, *, env=None):
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        **kwargs,
    )
    try:
        while proc.returncode is None:
            if partial.exists() and partial.stat().st_size > limit:
                raise DeliveryError("压缩包超过大小上限；原文件已保留")
            try:
                await asyncio.wait_for(proc.wait(), 0.2)
            except TimeoutError:
                pass
        if proc.returncode != 0:
            raise DeliveryError("压缩或完整性校验失败，请检查压缩程序和磁盘空间")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _create_zip(manifest: dict, config: dict, password: str, timeout: int):
    output = Path(manifest["output"])
    partial = output.with_suffix(".partial")
    listfile = output.with_suffix(".list")
    descriptor = output.with_suffix(".json")
    kind, executable = engine(config, bool(password))
    try:
        async with asyncio.timeout(timeout):
            files = await asyncio.to_thread(validated_files, manifest)
            partial.unlink(missing_ok=True)
            if kind in ("builtin", "windows"):
                worker = [sys.executable, str(Path(__file__).with_name("archive_worker.py"))]
                if kind == "windows":
                    manifest = dict(
                        manifest, entries=[dict(source=str(path), name=name) for path, name in files]
                    )
                descriptor.write_text(json.dumps(manifest), encoding="utf-8")
                if kind == "windows":
                    await run_program(
                        [executable, "-NoProfile", "-NonInteractive", "-Command", WINDOWS_ZIP_COMMAND],
                        output.parent,
                        partial,
                        manifest["upload_bytes"],
                        env=dict(os.environ, ASTRBOT_MAGNET_ZIP_JOB=str(descriptor)),
                    )
                    worker.append("verify")
                await run_program(
                    [*worker, str(descriptor)],
                    output.parent,
                    partial,
                    manifest["upload_bytes"],
                )
                return
            if any("\n" in name or "\r" in name for _, name in files):
                raise DeliveryError("资源文件名包含换行，无法交给外部压缩程序")
            # Explicit files only; disable wildcards and recursion so no unrelated files enter ZIP.
            listfile.write_text("".join("./" + name + "\n" for _, name in files), encoding="utf-8")
            secret = ["-p" + password] if password else []
            args = [
                executable,
                "a",
                "-tzip",
                "-mx=1",
                "-y",
                "-bd",
                "-bb0",
                "-r-",
                "-spd",
                "-scsUTF-8",
                *secret,
            ]
            if password:
                args.append("-mem=AES256")
            await run_program(
                [*args, str(partial), "@" + str(listfile)],
                Path(manifest["root"]),
                partial,
                manifest["upload_bytes"],
            )
            if not partial.is_file() or partial.stat().st_size > manifest["upload_bytes"]:
                raise DeliveryError("压缩包超过大小上限或未生成；原文件已保留")
            with zipfile.ZipFile(partial) as archive:
                expected = {name: path.stat().st_size for path, name in files}
                entries = archive.infolist()
                if (
                    len(entries) != len(expected)
                    or {entry.filename: entry.file_size for entry in entries} != expected
                    or any(bool(entry.flag_bits & 1) != bool(password) for entry in entries)
                ):
                    raise DeliveryError("压缩包内容或加密状态校验失败")
            await run_program(
                [executable, "t", "-bd", "-bb0", "-y", *secret, str(partial)],
                output.parent,
                partial,
                manifest["upload_bytes"],
            )
            os.replace(partial, output)
    finally:
        for path in (partial, listfile, descriptor):
            path.unlink(missing_ok=True)


async def create_archive(manifest: dict, config: dict, password: str, timeout: int) -> list[Path]:
    output = Path(manifest["output"])
    part_size = manifest.get("volume_bytes")
    if part_size is None:
        await _create_zip(manifest, config, password, timeout)
        return [output]

    # A job still in compression can be replayed after a restart.
    pattern = re.compile(re.escape(output.name) + r"\.(?:split\.)?[0-9]+")
    for path in output.parent.iterdir():
        if pattern.fullmatch(path.name):
            path.unlink()
    staged = []
    published = []
    try:
        async with asyncio.timeout(timeout):
            await _create_zip(manifest, config, password, timeout)
            size = output.stat().st_size
            if size <= part_size:
                return [output]
            if shutil.disk_usage(output.parent).free < size + manifest.get("reserve_bytes", 0):
                raise DeliveryError("分卷需要额外磁盘空间，原文件和完整 ZIP 已保留")
            count = (size + part_size - 1) // part_size
            staged = [output.with_name(f"{output.name}.split.{i:03d}") for i in range(1, count + 1)]
            await run_program(
                [
                    sys.executable,
                    str(Path(__file__).with_name("archive_worker.py")),
                    "split",
                    str(output),
                    str(part_size),
                ],
                output.parent,
                staged[0],
                part_size,
            )
            for index, part in enumerate(staged):
                expected = min(part_size, size - index * part_size)
                if part.stat().st_size != expected:
                    raise DeliveryError("分卷大小校验失败，原文件已保留")
            for index, part in enumerate(staged, 1):
                final = output.with_name(f"{output.name}.{index:03d}")
                os.replace(part, final)
                published.append(final)
            output.unlink()
            return published
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
