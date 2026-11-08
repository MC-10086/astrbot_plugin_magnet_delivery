"""Run against real AstrBot imports when ASTRBOT_APP_PATH is set."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

APP = os.environ.get("ASTRBOT_APP_PATH")
if APP:
    sys.path.insert(0, APP)
    from test_core import FakeBackend

    from astrbot_plugin_magnet_delivery.core.configuration import load_profiles
    from astrbot_plugin_magnet_delivery.core.manager import Manager
    from astrbot_plugin_magnet_delivery.core.models import chat_key
    from astrbot_plugin_magnet_delivery.core.preview import PreviewService
    from astrbot_plugin_magnet_delivery.main import MagnetDelivery


@unittest.skipUnless(APP, "Set ASTRBOT_APP_PATH to test with real AstrBot")
class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.plugin = object.__new__(MagnetDelivery)
        self.plugin.config = {"download_backend": "qb"}
        self.plugin.download_backend = "qb"
        self.plugin.context = SimpleNamespace()
        self.plugin.preview = PreviewService()
        self.plugin.managed_aria2 = None
        profiles = {
            "qb": dict(kind="qb", url="http://qb", local_root=str(root / "qb")),
        }
        options = dict(
            concurrency=1,
            max_queue=50,
            max_per_chat=5,
            max_bytes=10000,
            upload_bytes=10000,
            max_files=100,
            reserve_bytes=0,
            poll_seconds=0.01,
            task_timeout=3600,
            archive_timeout=10,
            upload_timeout=1,
        )

        async def noop(*args):
            pass

        self.plugin.manager = Manager(root / "data", profiles, options, noop, noop)
        for name, backend in list(self.plugin.manager.backends.items()):
            await backend.close()
            self.plugin.manager.backends[name] = FakeBackend(root / name)

    async def asyncTearDown(self):
        await self.plugin.preview.close()
        await self.plugin.manager.close()
        self.tmp.cleanup()

    def event(self, text, group="100", sender="alice", admin=False, platform="bot1"):
        return SimpleNamespace(
            message_str=text,
            get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: platform,
            get_group_id=lambda: group,
            get_sender_id=lambda: sender,
            unified_msg_origin=f"{platform}:GroupMessage:{group}",
            message_obj=SimpleNamespace(self_id="999"),
            is_admin=lambda: admin,
            plain_result=lambda message: message,
        )

    async def command(self, text, **kwargs):
        return "\n".join(
            [result async for result in self.plugin.magnet(self.event(text, **kwargs))]
        )

    async def test_help_alias_matches_after_builtin_and_lists_all_magnet_commands(self):
        from astrbot.api.event import MessageEventResult
        from astrbot.builtin_stars.builtin_commands.main import Main
        from astrbot.core.star.star_handler import star_handlers_registry

        event = SimpleNamespace(
            get_message_str=lambda: "help",  # AstrBot removes the wake prefix before dispatch.
            is_at_or_wake_command=True,
            set_extra=lambda *_: None,
            plain_result=lambda message: MessageEventResult().message(message),
        )
        handlers = [
            handler
            for handler in star_handlers_registry
            if handler.handler in (Main.help, MagnetDelivery.magnet_help)
            and all(rule.filter(event, {}) for rule in handler.event_filters)
        ]
        self.assertEqual(
            [handler.handler for handler in handlers], [Main.help, MagnetDelivery.magnet_help]
        )
        result = await self.plugin.magnet_help(event)
        self.assertFalse(result.use_t2i_)
        help_text = result.chain[0].text
        for action in (
            "backends",
            "test",
            "on",
            "auto",
            "delivery",
            "compress",
            "encrypt",
            "autosend",
            "autodelete",
            "add",
            "preview",
            "tasks",
            "status",
            "cancel",
            "send",
            "resend",
            "cleanup",
        ):
            self.assertIn(f"/magnet {action}", help_text)
        self.assertEqual(help_text, await self.command("/magnet help"))

    async def test_plugin_backend_applies_to_all_chats_and_tasks_remain_isolated(self):
        self.assertIn("启用", await self.command("/magnet on", admin=True))
        self.plugin.manager.store.set_setting(chat_key("bot1", "100", "alice"), "backend", "aria")
        self.assertIn("插件配置", await self.command("/magnet backend aria", admin=True))
        command = "/magnet add magnet:?xt=urn:btih:" + "a" * 40
        self.assertIn("插件选定", await self.command(command + " aria"))
        self.assertIn("后端 qb", await self.command(command))
        self.assertIn("[qb]", await self.command("/magnet tasks", sender="bob"))
        self.assertNotIn("[qb]", await self.command("/magnet tasks", group="200"))
        task = self.plugin.manager.store.list()[0]
        response = await self.command("/magnet cancel " + task["id"], group="200", admin=True)
        self.assertIn("当前聊天", response)
        # Owner permission test without granting QQ roles when API lookup fails.
        response = await self.command("/magnet cancel " + task["id"], sender="bob")
        self.assertIn("只能操作自己的任务", response)
        await self.command("/magnet cancel " + task["id"])
        await self.plugin.manager.running[task["id"]]
        self.assertEqual(self.plugin.manager.store.get(task["id"])["state"], "cancelled")
        group_b = chat_key("bot1", "200", "alice")
        self.assertNotIn("backend", self.plugin.manager.store.settings(group_b))

    async def test_group_delivery_permissions_overrides_and_per_task_flags(self):
        self.assertIn("需要", await self.command("/magnet compress off"))
        self.assertIn("不压缩", await self.command("/magnet compress off", admin=True))
        self.assertIn("仅保存本机", await self.command("/magnet autosend off", admin=True))
        self.assertIn("自动压缩", await self.command("/magnet delivery", group="200"))
        await self.command("/magnet on", admin=True)
        link = "magnet:?xt=urn:btih:" + "f" * 40
        result = await self.command("/magnet add " + link + " --compress --no-send")
        self.assertIn("自动压缩", result)
        task = self.plugin.manager.store.list()[0]
        self.assertTrue(task["delivery"]["compress"])
        self.assertFalse(task["delivery"]["auto_send"])
        self.assertIn("需要自动压缩", await self.command("/magnet encrypt on", admin=True))
        self.assertIn("重复", await self.command("/magnet add " + link + " --send --no-send"))
        self.assertIn("自动压缩", await self.command("/magnet compress default", admin=True))

    async def test_delete_after_send_is_optional_and_requires_auto_send(self):
        self.assertIn("需要", await self.command("/magnet autodelete on"))
        await self.command("/magnet autosend off", admin=True)
        self.assertIn("请先执行", await self.command("/magnet autodelete on", admin=True))
        await self.command("/magnet on", admin=True)
        link = "magnet:?xt=urn:btih:" + "7" * 40
        self.assertIn(
            "先开启自动发送", await self.command("/magnet add " + link + " --delete-after-send")
        )
        self.assertEqual(self.plugin.manager.store.list(), [])
        await self.command("/magnet autosend on", admin=True)
        self.assertIn("发送成功后清理", await self.command("/magnet autodelete on", admin=True))
        await self.command("/magnet add " + link)
        task = self.plugin.manager.store.list()[0]
        self.assertTrue(task["delivery"]["delete_after_send"])
        self.assertIn("发送成功后清理本地文件", await self.command("/magnet delivery", group="200"))
        await self.command("/magnet autosend off", admin=True)
        self.assertIn("保留本地文件", await self.command("/magnet delivery"))

    async def test_manual_send_owner_permission_and_chat_scope(self):
        await self.command("/magnet on", admin=True)
        await self.command("/magnet add magnet:?xt=urn:btih:" + "9" * 40 + " --no-send")
        task = self.plugin.manager.store.list()[0]
        await self.plugin.manager._run(task)
        self.assertIn("本机已就绪", await self.command("/magnet tasks"))
        self.assertIn("只能操作", await self.command("/magnet send " + task["id"], sender="bob"))
        self.assertIn("当前聊天", await self.command("/magnet send " + task["id"], group="200"))
        self.assertIn("排队发送", await self.command("/magnet send " + task["id"]))

    async def test_raw_upload_keeps_original_name_and_archive_uses_task_name(self):
        from unittest.mock import AsyncMock

        client = SimpleNamespace(call_action=AsyncMock())
        self.plugin._client = lambda _: client
        task = dict(id="1" * 32, platform="bot1", group="100", delivery=dict(compress=False))
        path = Path(self.tmp.name) / "original.txt"
        await self.plugin._upload(task, path)
        self.assertEqual(client.call_action.call_args.kwargs["name"], "original.txt")
        task["delivery"]["compress"] = True
        await self.plugin._upload(task, path)
        self.assertEqual(client.call_action.call_args.kwargs["name"], "magnet_11111111.zip")
        await self.plugin._upload(task, path.with_name(f"{task['id']}.zip.001"))
        self.assertEqual(client.call_action.call_args.kwargs["name"], "magnet_11111111.zip.001")

    async def test_group_upload_uses_task_timeout_on_the_existing_onebot_connection(self):
        from unittest.mock import AsyncMock, patch

        from aiocqhttp import CQHttp
        from aiocqhttp.api_impl import ResultStore
        from aiocqhttp.exceptions import NetworkError

        client = CQHttp(api_timeout_sec=180)
        socket = SimpleNamespace(send=AsyncMock())
        client._wsr_api_clients["999"] = socket
        self.plugin._client = lambda _: client
        self.plugin.manager.options["upload_timeout"] = 1800
        task = dict(id="1" * 32, platform="bot1", group="100", self_id="999")
        path = Path(self.tmp.name) / "archive.zip"
        with patch.object(ResultStore, "fetch", new_callable=AsyncMock) as fetch:
            fetch.return_value = {"status": "ok", "data": {"file_id": "test"}}
            await self.plugin._upload(task, path)
            self.assertEqual(fetch.await_args.args[1], 1800)
            socket.send.assert_awaited_once()
            self.assertEqual(client._api._wsr_api._timeout_sec, 180)
            fetch.side_effect = NetworkError("WebSocket API call timeout")
            with self.assertRaises(TimeoutError):
                await self.plugin._upload(task, path)

    async def test_auto_delete_upload_requires_successful_receipt_but_normal_upload_stays_compatible(
        self,
    ):
        from unittest.mock import AsyncMock

        client = SimpleNamespace(call_action=AsyncMock(return_value=None))
        self.plugin._client = lambda _: client
        task = dict(
            id="1" * 32,
            platform="bot1",
            group="100",
            delivery=dict(compress=True, auto_send=True, delete_after_send=True),
        )
        path = Path(self.tmp.name) / "archive.zip"
        with self.assertRaisesRegex(ValueError, "file_id"):
            await self.plugin._upload(task, path)
        for result in (
            {"file_id": "confirmed-id"},
            {"status": "ok", "retcode": 0, "data": {"file_id": "confirmed-id"}},
        ):
            client.call_action.return_value = result
            await self.plugin._upload(task, path)
        client.call_action.return_value = {"status": "async", "data": {"file_id": "pending-id"}}
        with self.assertRaises(ValueError):
            await self.plugin._upload(task, path)
        task["delivery"]["delete_after_send"] = False
        client.call_action.return_value = None
        await self.plugin._upload(task, path)

    async def test_connection_test_checks_permission_and_reports_managed_health(self):
        from unittest.mock import AsyncMock

        self.plugin.managed_aria2 = SimpleNamespace(
            health=AsyncMock(return_value="自动管理：运行正常")
        )
        self.plugin.download_backend = "aria2_auto"
        self.assertIn("需要", await self.command("/magnet test aria2"))
        self.plugin.managed_aria2.health.assert_not_awaited()
        self.assertIn("运行正常", await self.command("/magnet test aria2", admin=True))
        self.assertIn("运行正常", await self.command("/magnet backends"))

    async def test_only_selected_downloader_starts_and_previous_task_keeps_its_backend(self):
        from unittest.mock import patch

        from astrbot_plugin_magnet_delivery.core.configuration import configured_profiles
        from astrbot_plugin_magnet_delivery.core.manager import identity
        from astrbot_plugin_magnet_delivery.core.store import Store

        config = {"download_backend": "xunlei_cli", "aria2_auto": {"enabled": True}, "qb": {"enabled": True}}
        root = Path(self.tmp.name) / "plugin_data" / "astrbot_plugin_magnet_delivery"
        previous = configured_profiles(config, root, selected="qb")["qb"]
        store = Store(root / "tasks.sqlite3")
        task = store.add(dict(chat="chat", owner="user", backend="qb", infohash="f" * 40,
                              fingerprint=identity(previous)))
        task.update(state="ready", submitted=True)
        store.save(task)
        store.close()
        plugin = MagnetDelivery(
            SimpleNamespace(), config
        )
        with patch(
            "astrbot_plugin_magnet_delivery.main.get_astrbot_data_path", return_value=self.tmp.name
        ):
            try:
                await plugin.initialize()
                self.assertEqual(list(plugin.manager.backends), ["xunlei_cli"])
                self.assertIsNone(plugin.managed_aria2)
                self.assertEqual(plugin.manager.get_backend(task).config, previous)
            finally:
                await plugin.terminate()

    @unittest.skipUnless(os.environ.get("ARIA2_TEST_EXE"), "Needs real aria2")
    async def test_real_plugin_initialization_registers_managed_backend_and_closes_it(self):
        from unittest.mock import patch

        plugin = MagnetDelivery(
            SimpleNamespace(),
            {"aria2_auto": {"enabled": True, "executable": os.environ["ARIA2_TEST_EXE"]}},
        )
        with patch(
            "astrbot_plugin_magnet_delivery.main.get_astrbot_data_path", return_value=self.tmp.name
        ):
            try:
                await plugin.initialize()
                self.assertEqual(set(plugin.manager.backends), {"aria2"})
                self.assertEqual(
                    plugin.managed_aria2.download_dir,
                    Path(self.tmp.name)
                    / "plugin_data"
                    / "astrbot_plugin_magnet_delivery"
                    / "downloads",
                )
                self.assertEqual(plugin._default_backend(), "aria2")
                result = [
                    r async for r in plugin.magnet(self.event("/magnet test aria2", admin=True))
                ]
                self.assertIn("运行正常", result[0])
                child = plugin.managed_aria2.process
                self.assertEqual(plugin.manager.store.list(), [])
            finally:
                await plugin.terminate()
            self.assertIsNotNone(child.poll())

    async def test_failed_managed_start_keeps_plugin_commands_available(self):
        from unittest.mock import patch

        plugin = MagnetDelivery(
            SimpleNamespace(),
            {
                "aria2_auto": {
                    "enabled": True,
                    "executable": str(Path(self.tmp.name) / "missing.exe"),
                }
            },
        )
        with patch(
            "astrbot_plugin_magnet_delivery.main.get_astrbot_data_path", return_value=self.tmp.name
        ):
            try:
                await plugin.initialize()
                result = [r async for r in plugin.magnet(self.event("/magnet backends"))]
                self.assertIn("未找到", result[0])
                event = self.event("/magnet add magnet:?xt=urn:btih:" + "a" * 40)
                chat = plugin.route(event)["chat"]
                plugin.manager.store.set_setting(chat, "enabled", True)
                response = [r async for r in plugin.magnet(event)]
                self.assertIn("aria2 自动管理未启动", response[0])
                self.assertEqual(plugin.manager.store.list(), [])
            finally:
                await plugin.terminate()

    async def test_auto_is_opt_in_and_command_does_not_double_submit(self):
        link = "magnet:?xt=urn:btih:" + "b" * 40
        self.assertEqual([r async for r in self.plugin.auto_magnet(self.event(link))], [])
        await self.command("/magnet on", admin=True)
        await self.command("/magnet auto on", admin=True)
        command = self.event("/magnet add " + link)
        self.assertEqual([r async for r in self.plugin.auto_magnet(command)], [])
        result = [r async for r in self.plugin.auto_magnet(self.event("分享 " + link))]
        self.assertEqual(len(result), 1)
        self.assertEqual(len(self.plugin.manager.store.list()), 1)

    async def test_wrong_platform_group_and_config_rejected(self):
        response = await self.command("/magnet on", group="", admin=True)
        self.assertIn("仅支持", response)
        with self.assertRaises(ValueError):
            load_profiles('[{"name":"bad","kind":"shell","local_root":"/tmp"}]')

    async def test_preview_without_downloader_and_no_task_submission(self):
        import httpx

        await self.plugin.preview.http.aclose()
        self.plugin.preview.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"name": "Example", "size": 1024, "count": 1}
                )
            )
        )
        # Preview is independent of the download on/off switch.
        response = await self.command("/magnet preview magnet:?xt=urn:btih:" + "c" * 40)
        self.assertIn("未创建下载任务", response)
        self.assertIn("Example", response)
        self.assertEqual(self.plugin.manager.store.list(), [])

    async def test_preview_continues_after_onebot_rejects_a_screenshot(self):
        from unittest.mock import AsyncMock, patch

        from aiocqhttp.exceptions import ActionFailed

        urls = [f"https://whatslink.info/image/{i}" for i in range(3)]
        result = {
            "hash": "a" * 40, "name": "test", "file_type": "video",
            "size": 100, "count": 1, "screenshots": urls,
        }
        event = self.event("/magnet preview magnet:?xt=urn:btih:" + "a" * 40)
        event.send = AsyncMock(side_effect=[None, ActionFailed({"retcode": 1200}), None])
        with (
            patch.object(self.plugin.preview, "lookup", return_value=result),
            patch.object(self.plugin.preview, "image", return_value=b"test image") as fetch,
        ):
            responses = [item async for item in self.plugin.magnet(event)]
        self.assertEqual(len(responses), 1)
        self.assertIn(urls[-1], responses[0])
        self.assertEqual([item.args[0] for item in fetch.await_args_list], urls)
        self.assertEqual(event.send.await_count, 3)

    async def test_preview_task_id_remains_chat_scoped_and_respects_whitelist(self):
        route = self.plugin.route(self.event(""))
        task = await self.plugin.manager.submit(route, "magnet:?xt=urn:btih:" + "d" * 40, "qb")
        response = await self.command("/magnet preview " + task["id"], group="200", admin=True)
        self.assertIn("当前聊天", response)
        self.plugin.config["allowed_groups"] = ["999"]
        response = await self.command("/magnet preview magnet:?xt=urn:btih:" + "d" * 40)
        self.assertIn("允许列表", response)


if __name__ == "__main__":
    unittest.main()
