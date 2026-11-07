"""Preference snapshots, real AES archives, and subprocess cancellation."""

import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_magnet_delivery.core.archiver import create_archive
from astrbot_plugin_magnet_delivery.core.delivery import (
    configuration,
    describe,
    engine,
    find_7zip,
    snapshot,
    task_password,
)

EXE = os.environ.get("SEVENZIP_TEST_EXE", "")


class PreferenceTests(unittest.TestCase):
    def test_default_and_fallback_without_supported_program(self):
        config = configuration()
        with patch.dict(os.environ, {"SystemRoot": ""}), patch(
            "astrbot_plugin_magnet_delivery.core.delivery.find_7zip", return_value=""
        ) as find:
            self.assertEqual(
                snapshot(config, {}),
                dict(compress=True, encrypt=False, auto_send=True, delete_after_send=True),
            )
            self.assertEqual(engine(config, False), ("builtin", ""))
            find.assert_not_called()
            with self.assertRaisesRegex(ValueError, "未找到可用的压缩软件"):
                engine(config, True)

    def test_password_requirements_and_no_plaintext_in_snapshot(self):
        config = configuration(dict(archiver="builtin", encrypt=True, password="test-secret"))
        with self.assertRaisesRegex(ValueError, "不支持"):
            snapshot(config, {})
        config["archiver"] = "auto"
        with patch("astrbot_plugin_magnet_delivery.core.delivery.find_7zip", return_value="7z"):
            result = snapshot(config, {})
            self.assertNotIn("test-secret", json.dumps(result))
            self.assertNotIn("test-secret", describe(result))
            self.assertEqual(task_password({"delivery": result}, config), "test-secret")
            with self.assertRaisesRegex(ValueError, "不一致"):
                task_password({"delivery": result}, dict(config, password="different"))
            config["password"] = ""
            with self.assertRaisesRegex(ValueError, "填写"):
                snapshot(config, {})
            for invalid in ("中文密码", "x" * 100, "line\nbreak", 'double"quote'):
                config["password"] = invalid
                with self.assertRaisesRegex(ValueError, "1至99"):
                    snapshot(config, {})

    def test_no_raw_encryption_or_arbitrary_override(self):
        with self.assertRaisesRegex(ValueError, "需要自动压缩"):
            snapshot(configuration(), {}, dict(compress=False, encrypt=True))
        with self.assertRaises(ValueError):
            snapshot(configuration(), {}, dict(password="secret"))
        with self.assertRaisesRegex(ValueError, "路径无效"):
            find_7zip("7z.exe -arbitrary")

    def test_delete_after_send_dependency_and_safe_inheritance(self):
        config = configuration(dict(archiver="builtin", delete_after_send=True))
        self.assertTrue(snapshot(config, {})["delete_after_send"])
        self.assertFalse(snapshot(config, {}, {"auto_send": False})["delete_after_send"])
        self.assertFalse(snapshot(config, {"delivery": {"auto_send": False}})["delete_after_send"])
        with self.assertRaisesRegex(ValueError, "先开启自动发送"):
            snapshot(config, {}, {"auto_send": False, "delete_after_send": True})
        self.assertIn("保留本地文件", describe(snapshot(config, {}, {"auto_send": False})))


@unittest.skipUnless(engine({}, False)[0] == "windows", "Needs Windows PowerShell")
class WindowsZipTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_system_zip_preserves_explicit_files_and_can_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "extra.txt").write_bytes(b"not selected")
            names = ["中文 [文件].txt", ".hidden"]
            for name in names:
                (source / name).write_bytes(b"hello")
            output = root / "archive.zip"
            manifest = dict(
                root=str(source), files=[(str(source / name), 5) for name in names],
                output=str(output), max_bytes=4096, upload_bytes=4096, volume_bytes=100,
            )
            with patch("astrbot_plugin_magnet_delivery.core.delivery.find_7zip") as find:
                parts = await create_archive(manifest, configuration(), "", 20)
                find.assert_not_called()
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(part.stat().st_size <= 100 for part in parts))
            output.write_bytes(b"".join(part.read_bytes() for part in parts))
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), names)
                self.assertEqual([archive.read(name) for name in names], [b"hello", b"hello"])


@unittest.skipUnless(EXE, "Set SEVENZIP_TEST_EXE for actual 7-Zip tests")
class RealArchiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.file = self.source / "中文 文件.txt"
        self.file.write_bytes(b"hello world")
        self.config = configuration(dict(archiver="7zip", executable=EXE))
        self.manifest = dict(
            root=str(self.source),
            files=[(str(self.file), 11)],
            output=str(self.root / "output.zip"),
            max_bytes=100000000,
            upload_bytes=100000000,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_real_aes_zip_password_and_explicit_file_list(self):
        (self.source / "not-selected.txt").write_text("must not be archived")
        special = self.source / "@list-name.txt"
        special.write_bytes(b"x")
        self.manifest["files"].append((str(special), 1))
        password = "test-only-$42 & pass"
        await create_archive(self.manifest, self.config, password, 15)
        with zipfile.ZipFile(self.manifest["output"]) as archive:
            self.assertEqual(set(archive.namelist()), {self.file.name, special.name})
            self.assertTrue(all(entry.flag_bits & 1 for entry in archive.infolist()))
            with self.assertRaises(RuntimeError):
                archive.read(self.file.name)
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        for supplied, success in (("wrong-password", False), (password, True)):
            proc = await asyncio.create_subprocess_exec(
                EXE,
                "x",
                "-so",
                "-p" + supplied,
                self.manifest["output"],
                self.file.name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **kwargs,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
            self.assertEqual(proc.returncode == 0, success)
            if success:
                self.assertEqual(stdout, b"hello world")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["input", "output.zip"])

    async def test_external_plain_zip_and_size_limit_cleanup(self):
        await create_archive(self.manifest, self.config, "", 15)
        with zipfile.ZipFile(self.manifest["output"]) as archive:
            self.assertEqual(archive.read(self.file.name), b"hello world")
        Path(self.manifest["output"]).unlink()
        self.manifest["upload_bytes"] = 2
        with self.assertRaises(ValueError):
            await create_archive(self.manifest, self.config, "", 15)
        self.assertEqual([p.name for p in self.root.iterdir()], ["input"])

    async def test_encrypted_volumes_can_be_extracted_by_opening_first_part(self):
        self.manifest["volume_bytes"] = 100
        password = "test-only"
        parts = await create_archive(self.manifest, self.config, password, 15)
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0].name, "output.zip.001")
        self.assertTrue(all(0 < part.stat().st_size <= 100 for part in parts))
        self.assertEqual(set(self.root.iterdir()), {self.source, *parts})
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        proc = await asyncio.create_subprocess_exec(
            EXE,
            "x",
            "-so",
            "-p" + password,
            str(parts[0]),
            self.file.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, self.file.read_bytes())

    async def test_cancel_kills_the_compressor_and_removes_temporary_files(self):
        spawned, processes = asyncio.Event(), []
        original = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            spawned.set()
            return proc

        with patch(
            "astrbot_plugin_magnet_delivery.core.archiver.asyncio.create_subprocess_exec", spawn
        ):
            job = asyncio.create_task(create_archive(self.manifest, self.config, "", 15))
            await asyncio.wait_for(spawned.wait(), 5)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertTrue(processes)
        self.assertTrue(all(proc.returncode is not None for proc in processes))
        self.assertEqual([p.name for p in self.root.iterdir()], ["input"])
