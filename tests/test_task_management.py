"""Cancellation cleanup and dashboard routing, including a real isolated aria2 instance."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_core import MAGNET, FakeBackend

from astrbot_plugin_magnet_delivery.core.cleanup import cleanup_task
from astrbot_plugin_magnet_delivery.core.manager import Manager
from astrbot_plugin_magnet_delivery.core.task_view import listing


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.options = dict(
            concurrency=2,
            max_queue=50,
            max_per_chat=5,
            max_bytes=100000,
            upload_bytes=100000,
            max_files=100,
            reserve_bytes=0,
            poll_seconds=0.01,
            task_timeout=100,
            archive_timeout=10,
            upload_timeout=1,
            delivery={"archiver": "builtin"},
        )
        profile = dict(kind="qb", url="http://qb", local_root=str(self.root / "downloads"))
        self.manager = Manager(
            self.root / "data", {"qb": profile}, self.options, AsyncMock(), AsyncMock()
        )
        await self.manager.backends["qb"].close()
        self.backend = FakeBackend(self.root / "downloads", complete=False)
        self.manager.backends["qb"] = self.backend
        self.route = dict(
            chat='["botA","group","123"]', owner="alice", platform="botA", group="123", umo="test"
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def prepared(self):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.backend.start(task)
        task.update(state="failed", submitted=True, remote_id="owned")
        self.manager.store.save(task)
        archives = self.manager.root / "archives"
        archives.mkdir(exist_ok=True)
        for suffix in (".zip", ".partial", ".json", ".list", ".partial.tmp"):
            (archives / (task["id"] + suffix)).write_bytes(b"test artifact")
        current = self.backend.root / (task["id"] + ".archives")
        current.mkdir()
        (current / (task["id"] + ".zip.001")).write_bytes(b"test volume")
        (self.backend.root / task["id"] / "hello.txt.aria2").write_bytes(b"control")
        return task

    async def cancel_and_wait(self, task):
        await self.manager.cancel(task)
        await self.manager.running[task["id"]]
        return self.manager.store.get(task["id"])

    async def test_cancel_removes_all_owned_files_but_not_sibling_tasks(self):
        task = await self.prepared()
        sibling = self.backend.root / ("b" * 32)
        sibling.mkdir()
        (sibling / "keep.txt").write_bytes(b"keep")
        other_archive = self.manager.root / "archives" / ("b" * 32 + ".zip")
        other_archive.write_bytes(b"keep archive")
        saved = await self.cancel_and_wait(task)
        self.assertEqual(saved["state"], "cancelled")
        self.assertTrue(saved["cleaned"])
        self.assertFalse((self.backend.root / task["id"]).exists())
        self.assertFalse((self.backend.root / (task["id"] + ".archives")).exists())
        self.assertFalse(
            any(path.name.startswith(task["id"]) for path in other_archive.parent.iterdir())
        )
        self.assertEqual((sibling / "keep.txt").read_bytes(), b"keep")
        self.assertTrue(other_archive.exists())
        for key in ("magnet", "files", "artifacts", "remote_id", "download_root"):
            self.assertNotIn(key, saved)

    async def test_backend_failure_does_not_delete_files_and_can_retry(self):
        task = await self.prepared()
        original = self.backend.cancel
        self.backend.cancel = AsyncMock(side_effect=OSError("test connection failure"))
        saved = await self.cancel_and_wait(task)
        self.assertEqual(saved["state"], "cancel_failed")
        self.assertTrue((self.backend.root / task["id"]).exists())
        self.assertNotIn("cleaned", saved)
        self.backend.cancel = original
        saved = await self.cancel_and_wait(saved)
        self.assertTrue(saved["cleaned"])

    async def test_cleanup_failure_is_visible_and_retry_does_not_repeat_remote_delete(self):
        task = await self.prepared()
        with patch(
            "astrbot_plugin_magnet_delivery.core.manager.cleanup_task",
            side_effect=PermissionError(),
        ):
            saved = await self.cancel_and_wait(task)
        self.assertEqual(saved["state"], "cleanup_failed")
        self.assertTrue(saved["remote_removed"])
        view = listing(self.manager)
        self.assertEqual(view["summary"]["attention"], 1)
        self.assertTrue(view["groups"][0]["tasks"][0]["cancellable"])
        saved = await self.cancel_and_wait(saved)
        self.assertTrue(saved["cleaned"])
        self.assertEqual(self.backend.cancels, [task["id"]])

    async def test_cancel_settles_inflight_add_before_removal(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.backend.start

        async def delayed(task):
            started.set()
            await release.wait()
            return await original(task)

        self.backend.start = delayed
        task = await self.manager.submit(self.route, MAGNET, "qb")
        job = asyncio.create_task(self.manager._run(task))
        self.manager.running[task["id"]] = job
        await started.wait()
        await self.manager.cancel(task)
        await asyncio.sleep(0.02)
        self.assertEqual(self.backend.cancels, [])
        release.set()
        await self.manager.running[task["id"]]
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.assertEqual(self.backend.cancels, [task["id"]])
        self.assertFalse((self.backend.root / task["id"]).exists())

    async def test_cancel_before_scheduled_job_starts_never_submits_download(self):
        self.backend.start = AsyncMock(side_effect=AssertionError("must not submit"))
        task = await self.manager.submit(self.route, MAGNET, "qb")
        self.manager.running[task["id"]] = asyncio.create_task(self.manager._run(task))
        await self.manager.cancel(task)
        await self.manager.running[task["id"]]
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.backend.start.assert_not_awaited()

    async def test_repeated_cancel_uses_same_cleanup_and_upload_is_rejected(self):
        task = await self.prepared()
        await self.manager.cancel(task)
        cleanup = self.manager.running[task["id"]]
        await self.manager.cancel(task)
        self.assertIs(self.manager.running[task["id"]], cleanup)
        await cleanup
        other = await self.prepared()
        other["state"] = "uploading"
        self.manager.store.save(other)
        with self.assertRaisesRegex(ValueError, "上传"):
            await self.manager.cancel(other)
        self.assertTrue((self.backend.root / other["id"]).exists())

    async def test_restart_resumes_pending_cleanup_and_never_restarts_download(self):
        task = await self.prepared()
        task.update(state="cancelling", cancel=True)
        self.manager.store.save(task)
        self.backend.start = AsyncMock(side_effect=AssertionError("must not redownload"))
        self.manager.start()
        for _ in range(100):
            if self.manager.store.get(task["id"])["state"] == "cancelled":
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.backend.start.assert_not_awaited()

    async def test_dashboard_groups_instances_and_omits_secrets_and_paths(self):
        await self.manager.submit(self.route, MAGNET, "qb")
        other = await self.manager.submit(
            dict(self.route, chat='["botB","group","123"]', platform="botB"),
            MAGNET.replace("a" * 40, "b" * 40),
            "qb",
        )
        other["name"] = '<img src=x onerror="bad">'
        other["private"] = "must-not-display"
        self.manager.store.save(other)
        result = listing(self.manager)
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["summary"]["active"], 2)
        serialized = json.dumps(result)
        for secret in (
            "magnet:?",
            "must-not-display",
            str(self.root),
            "fingerprint",
            "password_tag",
        ):
            self.assertNotIn(secret, serialized)

    @unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs real AstrBot")
    async def test_api_auth_scope_validation_and_cancel(self):
        from astrbot_plugin_magnet_delivery.main import MagnetDelivery

        plugin = object.__new__(MagnetDelivery)
        plugin.manager = self.manager
        with patch("astrbot_plugin_magnet_delivery.main.request", SimpleNamespace(username=None)):
            self.assertEqual((await plugin.tasks_get()).status_code, 401)
            self.assertEqual((await plugin.tasks_cancel()).status_code, 401)
        task = await self.prepared()
        for body, expected in (
            ({}, 400),
            ({"id": task["id"], "chat": "other-group"}, 404),
            ({"id": "../escape", "chat": task["chat"]}, 400),
            ({"id": task["id"], "chat": task["chat"]}, 202),
        ):
            req = SimpleNamespace(username="admin", json=AsyncMock(return_value=body))
            with patch("astrbot_plugin_magnet_delivery.main.request", req):
                self.assertEqual((await plugin.tasks_cancel()).status_code, expected)
        await self.manager.running[task["id"]]
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])

    @unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs real AstrBot")
    async def test_sent_cleanup_retry_api_requires_auth_and_never_uploads(self):
        from astrbot_plugin_magnet_delivery.main import MagnetDelivery

        plugin = object.__new__(MagnetDelivery)
        plugin.manager = self.manager
        with patch("astrbot_plugin_magnet_delivery.main.request", SimpleNamespace(username=None)):
            self.assertEqual((await plugin.tasks_cleanup()).status_code, 401)
        task = await self.prepared()
        task.update(state="sent_cleanup_failed", delivered=True)
        task["delivery"].update(auto_send=True, delete_after_send=True)
        self.manager.store.save(task)
        result = listing(self.manager)
        self.assertTrue(result["groups"][0]["tasks"][0]["retry_cleanup"])
        self.assertFalse(result["groups"][0]["tasks"][0]["cancellable"])
        for chat, expected in (("other-chat", 404), (task["chat"], 202)):
            req = SimpleNamespace(
                username="admin", json=AsyncMock(return_value={"id": task["id"], "chat": chat})
            )
            with patch("astrbot_plugin_magnet_delivery.main.request", req):
                self.assertEqual((await plugin.tasks_cleanup()).status_code, expected)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.manager.upload.assert_not_awaited()

    @unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs real AstrBot")
    async def test_web_delivery_requires_correct_state_and_explicit_resend_confirmation(self):
        from astrbot_plugin_magnet_delivery.main import MagnetDelivery

        plugin = object.__new__(MagnetDelivery)
        plugin.manager = self.manager
        with patch("astrbot_plugin_magnet_delivery.main.request", SimpleNamespace(username=None)):
            self.assertEqual((await plugin.tasks_send()).status_code, 401)
            self.assertEqual((await plugin.tasks_resend()).status_code, 401)
        task = await self.prepared()
        task.update(state="ready")
        self.manager.store.save(task)
        body = {"id": task["id"], "chat": task["chat"]}

        async def body_during_reload(**kwargs):
            plugin.manager = None
            return body

        interrupted = SimpleNamespace(username="admin", json=body_during_reload)
        with patch("astrbot_plugin_magnet_delivery.main.request", interrupted):
            self.assertEqual((await plugin.tasks_send()).status_code, 503)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "ready")
        plugin.manager = self.manager
        req = SimpleNamespace(username="admin", json=AsyncMock(return_value=body))
        with patch("astrbot_plugin_magnet_delivery.main.request", req):
            req.json.return_value = dict(body, chat="other")
            self.assertEqual((await plugin.tasks_send()).status_code, 404)
            req.json.return_value = body
            self.assertEqual((await plugin.tasks_send()).status_code, 202)
            self.assertEqual((await plugin.tasks_send()).status_code, 409)
            saved = self.manager.store.get(task["id"])
            self.assertTrue(saved["manual_send"])
            saved.update(state="upload_unknown")
            self.manager.store.save(saved)
            self.assertEqual((await plugin.tasks_send()).status_code, 409)
            self.assertEqual((await plugin.tasks_resend()).status_code, 400)
            req.json.return_value = dict(body, confirmed=True)
            self.assertEqual((await plugin.tasks_resend()).status_code, 202)
            self.assertEqual((await plugin.tasks_resend()).status_code, 409)
        self.assertTrue(self.manager.store.get(task["id"])["resend"])

    @unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs real AstrBot")
    async def test_registered_routes_match_real_astrbot_dispatcher(self):
        from astrbot.api.star import Context
        from astrbot.dashboard.api.plugins import _match_registered_web_api

        from astrbot_plugin_magnet_delivery.main import MagnetDelivery

        context = SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda *args: Context.register_web_api(context, *args)
        plugin = MagnetDelivery(context, {})
        for route, method, handler in (
            ("tasks", "GET", plugin.tasks_get),
            ("tasks/cancel", "POST", plugin.tasks_cancel),
            ("tasks/cleanup", "POST", plugin.tasks_cleanup),
            ("tasks/send", "POST", plugin.tasks_send),
            ("tasks/resend", "POST", plugin.tasks_resend),
            ("paths", "GET", plugin.paths_get),
        ):
            result = _match_registered_web_api(
                context.registered_web_apis, "astrbot_plugin_magnet_delivery/" + route, method
            )
            self.assertEqual(result[0], handler)
        self.assertIsNone(
            _match_registered_web_api(
                context.registered_web_apis, "astrbot_plugin_magnet_delivery/tasks/cancel", "GET"
            )
        )

    @unittest.skipUnless(os.environ.get("ARIA2_TEST_EXE"), "Needs real aria2")
    async def test_failed_aria2_duplicate_does_not_return_after_restart(self):
        from astrbot_plugin_magnet_delivery.core.backends import Aria2
        from astrbot_plugin_magnet_delivery.core.managed_aria2 import ManagedAria2

        service = ManagedAria2(
            self.root / "managed", os.environ["ARIA2_TEST_EXE"], self.root / "aria-downloads"
        )
        await service.start()
        backend = Aria2(service.profile)
        self.manager.profiles["aria"] = service.profile
        self.manager.backends["aria"] = backend
        original = dict(id="e" * 32, remote_id="", magnet=MAGNET)
        try:
            original["remote_id"] = await backend.start(original)
            self.assertEqual(
                (await backend.rpc("tellStatus", original["remote_id"]))["status"], "active"
            )
            task = await self.manager.submit(self.route, MAGNET, "aria")
            await asyncio.wait_for(self.manager._run(task), 10)
            saved = self.manager.store.get(task["id"])
            self.assertEqual(saved["state"], "failed", saved["error"])
            self.assertIn("错误码 12", saved["error"])
            await service.close()
            self.assertNotIn(task["id"][:16], (service.root / "session.txt").read_text())
            await service.start()
            self.assertFalse(await backend.owned_gids(task))
            self.assertEqual(
                (await backend.rpc("tellStatus", original["remote_id"]))["status"], "active"
            )
        finally:
            await service.close()

    @unittest.skipUnless(os.environ.get("ARIA2_TEST_EXE"), "Needs real aria2")
    async def test_real_aria2_task_result_session_and_files_are_removed(self):
        from astrbot_plugin_magnet_delivery.core.backends import Aria2
        from astrbot_plugin_magnet_delivery.core.managed_aria2 import ManagedAria2

        service = ManagedAria2(
            self.root / "managed", os.environ["ARIA2_TEST_EXE"], self.root / "aria-downloads"
        )
        await service.start()
        backend = Aria2(service.profile)
        self.manager.profiles["aria"] = service.profile
        self.manager.backends["aria"] = backend
        try:
            task = await self.manager.submit(self.route, MAGNET, "aria")
            gid = task["id"][:16]
            await backend.rpc(
                "addUri",
                ["http://127.0.0.1:9/test.bin"],
                {"dir": backend.remote_dir(task), "gid": gid, "pause": "true"},
            )
            task.update(state="downloading", submitted=True, remote_id=gid)
            self.manager.store.save(task)
            directory = backend.root / task["id"]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "test.bin").write_bytes(b"partial")
            (directory / "test.bin.aria2").write_bytes(b"control")
            await backend.rpc("saveSession")
            unrelated = backend.root / "unrelated"
            unrelated.mkdir(parents=True, exist_ok=True)
            (unrelated / "keep.bin").write_bytes(b"keep")
            await backend.rpc(
                "addUri",
                ["http://127.0.0.1:9/keep.bin"],
                {"dir": str(unrelated), "gid": "f" * 16, "pause": "true"},
            )
            saved = await self.cancel_and_wait(task)
            self.assertEqual(saved["state"], "cancelled", saved["error"])
            self.assertFalse(directory.exists())
            self.assertEqual(await backend.rpc("tellActive"), [])
            self.assertEqual(await backend.rpc("tellStopped", 0, 100), [])
            self.assertNotIn(gid, (service.root / "session.txt").read_text())
            self.assertIn("f" * 16, (service.root / "session.txt").read_text())
            self.assertEqual((await backend.rpc("tellStatus", "f" * 16))["status"], "paused")
            self.assertEqual((unrelated / "keep.bin").read_bytes(), b"keep")
        finally:
            await service.close()


class CleanupBoundaryTests(unittest.TestCase):
    def test_rejects_non_task_target_and_linked_task_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            downloads = root / "downloads"
            downloads.mkdir()
            with self.assertRaises(ValueError):
                cleanup_task(data, downloads, "../downloads")
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_bytes(b"keep")
            link = downloads / ("a" * 32)
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symbolic link creation unavailable")
            with self.assertRaises(ValueError):
                cleanup_task(data, downloads, "a" * 32)
            self.assertEqual((outside / "keep.txt").read_bytes(), b"keep")
