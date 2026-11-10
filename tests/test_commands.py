"""Run against real AstrBot imports when ASTRBOT_APP_PATH is set."""

import asyncio
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
    from astrbot_plugin_magnet_delivery.core.models import chat_key, normalize_magnet
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
        self.plugin._forward_lock = asyncio.Lock()
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
        origin = f"{platform}:GroupMessage:{group}" if group else f"{platform}:FriendMessage:{sender}"
        event = SimpleNamespace(
            message_str=text,
            is_at_or_wake_command=False,
            call_llm=False,
            get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: platform,
            get_group_id=lambda: group,
            is_private_chat=lambda: not group,
            get_sender_id=lambda: sender,
            unified_msg_origin=origin,
            message_obj=SimpleNamespace(self_id="999"),
            is_admin=lambda: admin,
            plain_result=lambda message: message,
        )
        event.should_call_llm = lambda value: setattr(event, "call_llm", value)
        return event

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
            get_platform_name=lambda: "aiocqhttp",
            is_private_chat=lambda: False,
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

    async def test_private_upload_uses_only_authorized_task_owner(self):
        from unittest.mock import AsyncMock

        client = SimpleNamespace(call_action=AsyncMock(return_value={"file_id": "confirmed"}))
        self.plugin._client = lambda _: client
        self.plugin.context.send_message = AsyncMock()
        self.plugin.config.update(private_enabled=True, allowed_private_users=["123456"])
        task = dict(
            id="1" * 32, platform="bot1", group="", owner="123456", self_id="999",
            umo="bot1:FriendMessage:123456",
            delivery=dict(compress=True, auto_send=True, delete_after_send=True),
        )
        path = Path(self.tmp.name) / "archive.zip"
        await self.plugin._upload(task, path)
        client.call_action.assert_awaited_once_with(
            "upload_private_file", user_id="123456", file=str(path.resolve()),
            name="magnet_11111111.zip", self_id="999",
        )
        client.call_action.return_value = {"file_id": None}
        with self.assertRaisesRegex(ValueError, "file_id"):
            await self.plugin._upload(task, path)
        task["delivery"]["delete_after_send"] = False
        with self.assertRaisesRegex(ValueError, "file_id"):
            await self.plugin._upload(task, path)
        client.call_action.reset_mock()
        self.plugin.config["allowed_private_users"] = ["234567"]
        with self.assertRaisesRegex(ValueError, "私聊权限"):
            await self.plugin._upload(task, path)
        await self.plugin._notify(task, "progress")
        client.call_action.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()

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

    async def test_global_auto_download_respects_group_permission_and_override(self):
        self.plugin.config["auto_download"] = True
        event = self.event("分享磁力 magnet:?xt=urn:btih:" + "e" * 40)
        self.assertEqual([r async for r in self.plugin.auto_magnet(event)], [])
        await self.command("/magnet on", admin=True)
        await self.command("/magnet auto off", admin=True)
        self.assertEqual([r async for r in self.plugin.auto_magnet(event)], [])
        await self.command("/magnet on", admin=True, group="200")
        event = self.event(event.message_str, group="200")
        replies = [r async for r in self.plugin.auto_magnet(event)]
        self.assertEqual(len(replies), 1)
        self.assertIn("已排队", replies[0])
        self.assertEqual(len(self.plugin.manager.store.list()), 1)

    async def test_auto_skips_invalid_candidates_and_dedupes_before_applying_limit(self):
        self.plugin.config["auto_download"] = True
        await self.command("/magnet on", admin=True)
        first = "magnet:?xt=urn:btih:" + "a" * 40
        duplicate = first + "&dn=Duplicate"
        second = "magnet:?xt=urn:btih:" + "b" * 40
        third = "magnet:?xt=urn:btih:" + "c" * 40
        event = self.event(f"magnet:?xt=invalid [{first}] {duplicate} ({second}) {third}.")
        replies = [r async for r in self.plugin.auto_magnet(event)]
        self.assertEqual(len(replies), 3)
        self.assertEqual(
            [task["infohash"] for task in self.plugin.manager.store.list()], ["a" * 40, "b" * 40, "c" * 40]
        )

    async def test_auto_preview_is_independent_and_can_be_text_only(self):
        from unittest.mock import AsyncMock, patch

        link = "magnet:?xt=urn:btih:" + "a" * 40
        screenshot = "https://whatslink.info/image/1"
        result = {
            "hash": "a" * 40, "name": "Example", "file_type": "video",
            "size": 100, "count": 1, "screenshots": [screenshot],
        }
        event = self.event("磁力 " + link)
        event.send = AsyncMock()
        with (
            patch.object(self.plugin.preview, "lookup", return_value=result) as lookup,
            patch.object(self.plugin.preview, "image", return_value=b"image") as fetch,
        ):
            self.assertEqual([r async for r in self.plugin.auto_magnet(event)], [])
            lookup.assert_not_awaited()
            self.plugin.config["auto_preview"] = True
            blocked = self.event("magnet preview " + link)
            blocked.is_at_or_wake_command = True
            self.assertEqual([r async for r in self.plugin.auto_magnet(blocked)], [])
            self.plugin.config["allowed_groups"] = ["200"]
            self.assertEqual([r async for r in self.plugin.auto_magnet(event)], [])
            lookup.assert_not_awaited()
            self.plugin.config["allowed_groups"] = ["100"]
            replies = [r async for r in self.plugin.auto_magnet(event)]
            self.assertEqual(len(replies), 1)
            self.assertIn(screenshot, replies[0])
            event.send.assert_awaited_once()
            fetch.assert_awaited_once_with(screenshot)
            fetch.reset_mock()
            event.send.reset_mock()
            self.plugin.config["preview_inline_images"] = False
            self.assertIn(screenshot, [r async for r in self.plugin.auto_magnet(event)][0])
            self.assertIn(screenshot, await self.command("/magnet preview " + link))
            fetch.assert_not_awaited()
            event.send.assert_not_awaited()
        self.assertEqual(self.plugin.manager.store.list(), [])

    async def test_auto_scan_forward_opt_in_and_outer_text_only(self):
        from unittest.mock import AsyncMock, patch

        from astrbot.api.message_components import Forward

        def link(hash_char):
            return "magnet:?xt=urn:btih:" + hash_char * 40

        def text(hash_char):
            return {"type": "text", "data": {"text": link(hash_char)}}

        event = self.event("正文 " + link("a"))
        event.get_messages = lambda: [Forward(id="card-1"), Forward(id="card-2")]
        messages = [
            {"message": [text("A"), {"type": "image", "data": {"text": link("c")}}]},
            {"type": "node", "data": {"content": [
                {"type": "forward", "data": {"content": [{"message": [text("d")]}]}},
                text("b"),
            ]}},
            *([{}] * 97),
            {"message": [text("e")]},
        ]
        client = SimpleNamespace(call_action=AsyncMock(return_value={"messages": messages}))
        preview = {
            "hash": "a" * 40, "name": "Example", "file_type": "video",
            "size": 100, "count": 1, "screenshots": [],
        }
        self.plugin.config["auto_preview"] = True
        second_preview = dict(preview, hash="b" * 40, name="Second")
        with (
            patch.object(self.plugin, "_client", return_value=client),
            patch.object(
                self.plugin.preview, "lookup_batch", return_value=[preview, second_preview]
            ) as lookup,
        ):
            card_only = self.event("")
            card_only.get_messages = event.get_messages
            self.assertEqual([r async for r in self.plugin.auto_magnet(card_only)], [])
            client.call_action.assert_not_awaited()
            self.plugin.config["scan_forwarded_magnets"] = True
            self.plugin.config["allowed_groups"] = ["200"]
            self.assertEqual([r async for r in self.plugin.auto_magnet(card_only)], [])
            client.call_action.assert_not_awaited()
            self.plugin.config["allowed_groups"] = ["100"]
            self.assertEqual(len([r async for r in self.plugin.auto_magnet(card_only)]), 2)
            targets = lookup.await_args.args[0]
            self.assertEqual([normalize_magnet(link)[1] for link in targets], ["a" * 40, "b" * 40])
            self.assertEqual(self.plugin.manager.store.list(), [])
            client.call_action.reset_mock()
            await self.command("/magnet on", admin=True)
            await self.command("/magnet auto on", admin=True)
            replies = [r async for r in self.plugin.auto_magnet(event)]
            client.call_action.assert_awaited_once_with(
                "get_forward_msg", message_id="card-1", self_id="999"
            )
        self.assertEqual(len(replies), 4)  # Two queued downloads and two previews.
        self.assertEqual(
            [task["infohash"] for task in self.plugin.manager.store.list()], ["a" * 40, "b" * 40]
        )

    async def test_auto_preview_detects_two_links_even_when_they_touch(self):
        from unittest.mock import patch

        first = "magnet:?xt=urn:btih:" + "a" * 40
        second = "magnet:?xt=urn:btih:" + "b" * 40
        first_preview = {
            "hash": "a" * 40, "name": "First", "file_type": "video",
            "size": 100, "count": 1, "screenshots": [],
        }
        second_preview = dict(first_preview, hash="b" * 40, name="Second")
        self.plugin.config["auto_preview"] = True
        event = self.event(first + "," + second)
        with patch.object(
            self.plugin.preview, "lookup_batch", return_value=[first_preview, second_preview]
        ) as lookup:
            replies = [r async for r in self.plugin.auto_magnet(event)]
        targets = lookup.await_args.args[0]
        self.assertEqual([normalize_magnet(link)[1] for link in targets], ["a" * 40, "b" * 40])
        self.assertEqual(len(replies), 2)
        self.assertIn("First", replies[0])
        self.assertIn("Second", replies[1])

    async def test_forward_download_limit_default_custom_and_disabled(self):
        from unittest.mock import AsyncMock, patch

        from astrbot.api.message_components import Forward

        def link(char):
            return "magnet:?xt=urn:btih:" + char * 40

        self.plugin.config.update(auto_download=True, scan_forwarded_magnets=True)
        await self.command("/magnet on", admin=True)
        event = self.event("正文 " + link("a"))
        event.get_messages = lambda: [Forward(id="card")]
        messages = [
            {"message": [{"type": "text", "data": {"text": link(char)}}]}
            for char in "abcdef"
        ]
        client = SimpleNamespace(call_action=AsyncMock(return_value={"messages": messages}))
        with (
            patch.object(self.plugin, "_client", return_value=client),
            patch.object(
                self.plugin, "_submit_download", return_value={"id": "test", "backend": "qb"}
            ) as submit,
        ):
            for config, expected in (
                ({}, "abc"),
                ({"forward_auto_max_links": None}, "abc"),
                ({"forward_auto_max_links": 2}, "ab"),
                ({"forward_auto_limit_enabled": False}, "abcde"),
            ):
                with self.subTest(config=config):
                    self.plugin.config.update(config)
                    submit.reset_mock()
                    replies = [r async for r in self.plugin.auto_magnet(event)]
                    self.assertEqual(len(replies), len(expected))
                    hashes = [normalize_magnet(call.args[1])[1] for call in submit.await_args_list]
                    self.assertEqual(hashes, [char * 40 for char in expected])

    async def test_forward_api_failure_preserves_ordinary_download(self):
        from unittest.mock import AsyncMock, patch

        from aiocqhttp.exceptions import ActionFailed
        from astrbot.api.message_components import Forward

        self.plugin.config.update(auto_download=True, scan_forwarded_magnets=True)
        await self.command("/magnet on", admin=True)
        event = self.event("magnet:?xt=urn:btih:" + "b" * 40)
        event.get_messages = lambda: [Forward(id="expired-card")]
        client = SimpleNamespace(call_action=AsyncMock(side_effect=ActionFailed({"retcode": 1200})))
        with patch.object(self.plugin, "_client", return_value=client):
            replies = [r async for r in self.plugin.auto_magnet(event)]
        client.call_action.assert_awaited_once()
        self.assertEqual(len(replies), 1)
        self.assertIn("已排队", replies[0])

    async def test_auto_download_rechecks_group_setting_after_forward_lookup(self):
        from unittest.mock import AsyncMock, patch

        from astrbot.api.message_components import Forward

        self.plugin.config.update(auto_download=True, scan_forwarded_magnets=True)
        await self.command("/magnet on", admin=True)
        event = self.event("magnet:?xt=urn:btih:" + "b" * 40)
        event.get_messages = lambda: [Forward(id="card")]

        async def get_forward(*args, **kwargs):
            await self.command("/magnet off", admin=True)
            return {"messages": []}

        client = SimpleNamespace(call_action=AsyncMock(side_effect=get_forward))
        with patch.object(self.plugin, "_client", return_value=client):
            self.assertEqual([r async for r in self.plugin.auto_magnet(event)], [])
        self.assertEqual(self.plugin.manager.store.list(), [])

    async def test_auto_download_stops_submitting_batch_after_group_is_disabled(self):
        self.plugin.config["auto_download"] = True
        await self.command("/magnet on", admin=True)
        first = "magnet:?xt=urn:btih:" + "a" * 40
        second = "magnet:?xt=urn:btih:" + "b" * 40
        replies = self.plugin.auto_magnet(self.event(f"{first} {second}"))
        self.assertIn("已排队", await anext(replies))
        await self.command("/magnet off", admin=True)
        self.assertEqual([item async for item in replies], [])
        self.assertEqual(
            [task["infohash"] for task in self.plugin.manager.store.list()], ["a" * 40]
        )

    async def test_wrong_platform_group_and_config_rejected(self):
        response = await self.command("/magnet on", group="", admin=True)
        self.assertIn("私聊未启用", response)
        with self.assertRaises(ValueError):
            load_profiles('[{"name":"bad","kind":"shell","local_root":"/tmp"}]')

    async def test_private_commands_require_dashboard_whitelist_and_isolate_tasks(self):
        from unittest.mock import patch

        from astrbot_plugin_magnet_delivery.core.task_view import listing

        request = {"group": "", "sender": "123456"}
        link = "magnet:?xt=urn:btih:" + "a" * 40
        command = "/magnet add " + link
        self.plugin.config["allowed_private_users"] = ["123456"]
        self.assertIn("未获授权", await self.command(command, **request))
        self.plugin.config.update(private_enabled=True, allowed_private_users=[])
        self.assertIn("当前聊天任务", await self.command("/magnet tasks", **request))
        self.assertIn("当前聊天任务", await self.command("/magnet tasks", group="", sender="234567"))
        self.assertEqual(self.plugin.manager.store.list(), [])
        self.plugin.config.update(allowed_private_users=["123456", "234567"], allowed_groups=["100"])
        self.assertIn("仅用于群聊", await self.command("/magnet on", **request, admin=True))
        self.assertIn("已排队", await self.command(command, **request))
        task = self.plugin.manager.store.list()[0]
        self.assertEqual(task["umo"], "bot1:FriendMessage:123456")
        self.assertIn(task["id"][:8], await self.command("/magnet tasks", **request))
        for target in (
            {"group": "", "sender": "234567"},
            {"group": "100", "sender": "123456"},
            {**request, "platform": "bot2"},
        ):
            self.assertIn("当前聊天", await self.command("/magnet status " + task["id"], **target))
        self.assertIn("未获授权", await self.command("/magnet tasks", group="", sender="345678"))
        self.assertEqual(listing(self.plugin.manager)["groups"][0]["private_user"], "123456")

        preview = {
            "hash": "a" * 40, "name": "Example", "file_type": "video",
            "size": 100, "count": 1, "screenshots": [],
        }
        with patch.object(self.plugin.preview, "lookup", return_value=preview) as lookup:
            self.assertIn("Example", await self.command("/magnet preview " + link, **request))
            lookup.assert_awaited_once()

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
        from astrbot.api.event import MessageEventResult

        urls = [f"https://whatslink.info/image/{i}" for i in range(3)]
        result = {
            "hash": "a" * 40, "name": "test", "file_type": "video",
            "size": 100, "count": 1, "screenshots": urls,
        }
        event = self.event("/magnet preview magnet:?xt=urn:btih:" + "a" * 40)
        event.plain_result = lambda text: MessageEventResult().message(text)
        error = ActionFailed({"retcode": 1200})
        event.send = AsyncMock(side_effect=[error, error, None])
        with (
            patch.object(self.plugin.preview, "lookup", return_value=result),
            patch.object(self.plugin.preview, "image", return_value=b"test image") as fetch,
        ):
            responses = [item async for item in self.plugin.magnet(event)]
        self.assertEqual(len(responses), 2)
        self.assertIn(urls[-1], responses[0].chain[0].text)
        self.assertIn("「test」有 2 张预览图片发送失败或未确认", responses[1].chain[0].text)
        self.assertFalse(responses[1].use_t2i_)
        self.assertEqual([item.args[0] for item in fetch.await_args_list], urls)
        self.assertEqual(event.send.await_count, 3)

    async def test_merged_preview_sends_one_card_for_manual_and_automatic_requests(self):
        from unittest.mock import AsyncMock, patch

        link = "magnet:?xt=urn:btih:" + "a" * 40
        urls = [f"https://whatslink.info/image/{i}" for i in range(2)]
        result = {
            "hash": "a" * 40, "name": "Example", "file_type": "video",
            "size": 100, "count": 1, "screenshots": urls,
        }
        self.plugin.config.update(auto_preview=True, preview_merge_forward=True)
        with (
            patch.object(self.plugin.preview, "lookup", return_value=result),
            patch.object(self.plugin.preview, "image", side_effect=[None, b"image"]) as fetch,
        ):
            manual = self.event("/magnet preview " + link)
            manual.send = AsyncMock()
            self.assertEqual([r async for r in self.plugin.magnet(manual)], [])
            manual.send.assert_awaited_once()
            card = manual.send.await_args.args[0].chain[0]
            payload = await card.to_dict()
            nodes = payload["messages"]
            self.assertEqual(len(nodes), 2)
            self.assertEqual(nodes[0]["data"]["user_id"], "999")
            self.assertIn(urls[0], nodes[0]["data"]["content"][0]["data"]["text"])
            self.assertEqual(
                nodes[1]["data"]["content"],
                [{"type": "image", "data": {"file": "base64://aW1hZ2U="}}],
            )

            fetch.reset_mock()
            self.plugin.config["preview_inline_images"] = False
            automatic = self.event("分享 " + link)
            automatic.send = AsyncMock()
            self.assertEqual([r async for r in self.plugin.auto_magnet(automatic)], [])
            automatic.send.assert_awaited_once()
            card = automatic.send.await_args.args[0].chain[0]
            self.assertEqual(len(card.nodes), 1)
            fetch.assert_not_awaited()

    async def test_merged_preview_falls_back_to_plain_text_once(self):
        from unittest.mock import AsyncMock, patch

        from aiocqhttp.exceptions import ActionFailed, NetworkError
        from astrbot.api.event import MessageEventResult

        link = "magnet:?xt=urn:btih:" + "a" * 40
        screenshot = "https://whatslink.info/image/1"
        result = {
            "hash": "a" * 40, "name": "Example", "file_type": "video",
            "size": 100, "count": 1, "screenshots": [screenshot],
        }
        self.plugin.config["preview_merge_forward"] = True
        errors = (
            ActionFailed({"retcode": 1404}),
            ActionFailed({"retcode": 1200, "wording": "转发消息失败，生成节点为空"}),
            ActionFailed({"retcode": 1200, "message": "发送转发消息（res_id：example 失败"}),
            NetworkError("timeout"),
        )
        with (
            patch.object(self.plugin.preview, "lookup", return_value=result),
            patch.object(self.plugin.preview, "image", return_value=b"image"),
        ):
            for error in errors:
                with self.subTest(error=error):
                    event = self.event("/magnet preview " + link)
                    event.plain_result = lambda text: MessageEventResult().message(text)
                    event.send = AsyncMock(side_effect=error)
                    replies = [r async for r in self.plugin.magnet(event)]
                    self.assertEqual(len(replies), 1)
                    self.assertEqual(len(replies[0].chain), 1)
                    self.assertIn("Example", replies[0].chain[0].text)
                    self.assertIn(screenshot, replies[0].chain[0].text)
                    self.assertIn("带图片的预览发送失败或未确认", replies[0].chain[0].text)
                    self.assertFalse(replies[0].use_t2i_)
                    self.assertFalse(replies[0].use_markdown_)
                    event.send.assert_awaited_once()
                    self.assertTrue(event.call_llm)

            self.plugin.config["preview_inline_images"] = False
            replies = [r async for r in self.plugin.magnet(event)]
            self.assertEqual(len(replies), 1)
            self.assertNotIn("带图片的预览发送失败或未确认", replies[0].chain[0].text)

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
