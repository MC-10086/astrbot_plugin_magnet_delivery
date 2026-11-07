import json
import tempfile
import unittest
from datetime import datetime
from itertools import chain, repeat
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from astrbot_plugin_magnet_delivery.core.configuration import configured_profiles
from astrbot_plugin_magnet_delivery.core.delivery import DeliveryError
from astrbot_plugin_magnet_delivery.core.models import normalize_magnet
from astrbot_plugin_magnet_delivery.core.xunlei_cli import (
    CLIENT_COMMAND_FAILED,
    CLIENT_CONNECTION_TIMEOUT,
    CLIENT_NOT_RUNNING,
    CLI_RELATIVE,
    INVALID_TASK_ID,
    MISSING,
    XunleiCLI,
    XunleiCLIError,
    XunleiCLINotSubmitted,
)


class XunleiCLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name).resolve()
        self.root = self.data / "下载目录"
        self.folder = self.root / ("a" * 32)
        self.backend = XunleiCLI({"local_root": str(self.root)})
        magnet, infohash = normalize_magnet("magnet:?xt=urn:btih:" + "b" * 40)
        self.task = dict(id=self.folder.name, infohash=infohash, magnet=magnet, remote_id="123")
        self.row = dict(task_id="123", url=magnet, save_dir=str(self.folder), status="done",
                        total_size=4, downloaded_size=4, name="file.bin")
        self.backend.command = AsyncMock()

    def test_detects_already_installed_skill(self):
        executable = self.data / "skills" / "xunlei-cli-skill" / CLI_RELATIVE
        executable.parent.mkdir(parents=True)
        executable.touch()
        root = self.data / "plugin_data" / "test"
        profiles = configured_profiles({"xunlei_cli": {"enabled": True}}, root)
        self.assertEqual(profiles["xunlei_cli"]["executable"], str(executable))

    async def test_add_uses_isolated_directory_and_recovers_without_readding(self):
        self.backend.command.side_effect = [[], {"task_ids": ["123"]}, [self.row]]
        task = dict(self.task, remote_id="")
        self.assertEqual(await self.backend.start(task), "123")
        args = self.backend.command.await_args.args
        self.assertEqual(args[:2], ("+add", "--params"))
        self.assertEqual([c.args[0] for c in self.backend.command.await_args_list], ["list", "+add"])
        self.assertEqual(json.loads(args[2]), {"url": ["magnet:?xt=urn:btih:" + "b" * 40],
                                               "save_dir": str(self.folder)})
        self.assertEqual(await self.backend.start(dict(task, recovering=True)), "123")
        self.assertEqual(self.backend.command.await_args.args[0], "list")

    async def test_stopped_client_starts_once_and_connection_is_rechecked_before_add(self):
        self.backend.command.side_effect = [
            XunleiCLIError(CLIENT_NOT_RUNNING),
            XunleiCLIError(CLIENT_COMMAND_FAILED),  # The launch command can fail after opening the app.
            XunleiCLIError(CLIENT_COMMAND_FAILED),
            [],
            {"task_id": "123"},
        ]
        self.assertEqual(await self.backend.start(self.task), "123")
        calls = self.backend.command.await_args_list
        self.assertEqual([c.args[0] for c in calls], ["list", "launch", "list", "list", "+add"])
        self.assertEqual(calls[1], call("launch", "--wait", "10", app=True))

    async def test_failed_connection_after_launch_prevents_add(self):
        self.backend.command.side_effect = chain(
            [XunleiCLIError(CLIENT_NOT_RUNNING), None],
            repeat(XunleiCLIError(CLIENT_CONNECTION_TIMEOUT)),
        )
        with patch("astrbot_plugin_magnet_delivery.core.xunlei_cli.STARTUP_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(XunleiCLINotSubmitted, "就绪超时"):
                await self.backend.start(self.task)
        self.assertEqual(
            [c.args[0] for c in self.backend.command.await_args_list], ["list", "launch", "list"]
        )
        self.assertFalse(self.folder.exists())

    async def test_already_connected_client_with_transient_error_is_not_relaunched(self):
        self.backend.command.side_effect = [
            XunleiCLIError(CLIENT_COMMAND_FAILED), [], {"task_id": "123"},
        ]
        self.assertEqual(await self.backend.start(self.task), "123")
        self.assertEqual(
            [c.args[0] for c in self.backend.command.await_args_list], ["list", "list", "+add"]
        )

    async def test_not_logged_in_never_calls_add(self):
        self.backend.command.side_effect = XunleiCLIError(-32006)
        with self.assertRaisesRegex(XunleiCLINotSubmitted, "登录"):
            await self.backend.start(self.task)
        self.backend.command.assert_awaited_once()
        self.assertFalse(self.folder.exists())

    async def test_completed_snapshot_uses_local_files_even_when_reported_sizes_differ(self):
        subfolder = self.folder / "nested"
        subfolder.mkdir(parents=True)
        file = subfolder / "file.bin"
        file.write_bytes(b"data")
        self.backend.command.side_effect = [
            dict(self.row, total_size=10, downloaded_size=6),
            dict(self.row, file_errors=[{}]),
            self.row,
        ]
        status = await self.backend.poll(self.task)
        self.assertTrue(status.complete)
        self.assertEqual(status.files, [(str(file), 4)])
        self.assertEqual((status.total, status.done), (4, 4))
        with self.assertRaisesRegex(DeliveryError, "文件不完整"):
            await self.backend.poll(self.task)
        file.unlink()
        with self.assertRaisesRegex(DeliveryError, "没有可交付的文件"):
            await self.backend.poll(self.task)

    async def test_cancel_refuses_unrelated_task(self):
        self.backend.command.return_value = dict(self.row, save_dir=str(self.root / "other"))
        with self.assertRaisesRegex(DeliveryError, "无法确认迅雷任务归属"):
            await self.backend.cancel(self.task)
        self.backend.command.assert_awaited_once_with("detail", "--task-id", "123")

    async def test_preexisting_task_is_reported_and_never_removed(self):
        created = datetime(2026, 11, 8, 1, 3, 56).timestamp()
        task = dict(self.task, created=created)
        row = dict(self.row, save_dir=str(self.data / "personal"),
                   create_time="2026-11-07 19:48:55", status="pause")
        self.backend.command.return_value = row
        with self.assertRaisesRegex(DeliveryError, "已有同一资源的旧任务"):
            await self.backend.poll(task)
        self.backend.command.reset_mock()
        await self.backend.cancel(task)
        self.backend.command.assert_awaited_once_with("detail", "--task-id", "123")

        for change in (
            dict(create_time="2026-11-08 01:03:57"),
            dict(create_time=""),
            dict(save_dir=str(self.folder / "nested")),
            dict(save_dir=str(self.folder.parent)),
            dict(url="magnet:?xt=urn:btih:" + "c" * 40),
        ):
            with self.subTest(change=change):
                self.backend.command.reset_mock()
                self.backend.command.return_value = dict(row, **change)
                with self.assertRaisesRegex(DeliveryError, "无法确认迅雷任务归属"):
                    await self.backend.cancel(task)
                self.backend.command.assert_awaited_once_with("detail", "--task-id", "123")

    async def test_thunder_torrent_cache_source_still_identifies_the_magnet(self):
        self.folder.mkdir(parents=True)
        cache = r"C:\Temp\Thunder Network\Xmp6\xunlei\torrents" + "\\"
        source = cache + self.task["infohash"].upper() + ".torrent"
        row = dict(self.row, url=source, status="running")
        self.backend.command.side_effect = [row, [row], row, None, XunleiCLIError(MISSING), []]
        self.assertFalse((await self.backend.poll(self.task)).complete)
        self.assertEqual(await self.backend.find(self.task), "123")
        await self.backend.cancel(self.task)
        self.assertFalse(
            self.backend.matches(self.folder, dict(row, url=cache + "other.torrent"), self.task)
        )

    async def test_cancel_verifies_removal_and_leaves_local_cleanup_to_manager(self):
        self.folder.mkdir(parents=True)
        partial = self.folder / "file.part"
        partial.touch()
        self.backend.command.side_effect = [self.row, None, XunleiCLIError(MISSING), []]
        await self.backend.cancel(self.task)
        self.assertEqual(self.backend.command.await_args_list[1].args, ("remove", "--task-id", "123"))
        self.assertTrue(partial.exists())

    async def test_invalid_id_requires_task_list_to_confirm_removal(self):
        invalid = XunleiCLIError(INVALID_TASK_ID)
        self.backend.command.side_effect = [self.row, None, invalid, []]
        await self.backend.cancel(self.task)
        self.assertEqual(self.backend.command.await_args.args[0], "list")
        moved = dict(self.row, save_dir=str(self.root / "moved"))
        self.backend.command.side_effect = [invalid, [moved]]
        with self.assertRaisesRegex(DeliveryError, "无法确认迅雷任务归属"):
            await self.backend.cancel(self.task)

    async def test_replacement_id_is_recovered_for_poll_and_cancel_only_with_ownership(self):
        invalid = XunleiCLIError(INVALID_TASK_ID)
        replacement = dict(self.row, task_id="456")
        self.backend.command.side_effect = [invalid, [replacement], replacement]
        self.assertEqual((await self.backend.poll(self.task)).remote_id, "456")

        # Recheck ownership in detail before using a replacement ID for any mutation.
        self.backend.command.reset_mock()
        moved = dict(replacement, save_dir=str(self.root / "unrelated"))
        self.backend.command.side_effect = [invalid, [replacement], moved]
        with self.assertRaisesRegex(DeliveryError, "无法确认迅雷任务归属"):
            await self.backend.cancel(self.task)
        self.assertEqual(
            [entry.args[0] for entry in self.backend.command.await_args_list],
            ["detail", "list", "detail"],
        )

        self.backend.command.reset_mock()
        self.backend.command.side_effect = [invalid, [replacement], replacement, None, invalid, []]
        await self.backend.cancel(self.task)
        self.assertIn(call("remove", "--task-id", "456"), self.backend.command.await_args_list)
