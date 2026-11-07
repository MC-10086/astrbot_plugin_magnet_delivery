"""Delete-after-send requires a complete acknowledged batch, never an upload guess."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_core as fixtures

from astrbot_plugin_magnet_delivery.core.models import Snapshot


class AutoDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ManagerTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.ManagerTests.asyncTearDown(self)

    async def submit_run(self, overrides=None):
        task = await self.manager.submit(self.route, fixtures.MAGNET, "qb", overrides)
        await self.manager._run(task)
        return self.manager.store.get(task["id"])

    async def wait_completed(self, task):
        for _ in range(100):
            saved = self.manager.store.get(task["id"])
            if saved["state"] == "completed":
                return saved
            await asyncio.sleep(0.02)
        self.fail("task did not finish cleanup")

    async def test_success_cleans_download_archive_and_remote_task(self):
        task = await self.submit_run()
        self.assertEqual(task["state"], "completed")
        self.assertTrue(task["delivered"])
        self.assertTrue(task["cleaned"])
        self.assertEqual(task["cleanup_reason"], "after_send")
        self.assertEqual(self.uploads, [("groupA", task["id"])])
        self.assertEqual(self.fake.cancels, [task["id"]])
        self.assertFalse((self.fake.root / task["id"]).exists())
        self.assertFalse((self.fake.root / (task["id"] + ".archives")).exists())

    async def test_opt_out_and_legacy_tasks_keep_files(self):
        self.manager.delivery["delete_after_send"] = False
        task = await self.submit_run()
        self.assertTrue((self.fake.root / task["id"] / "hello.txt").is_file())
        archive = self.fake.root / (task["id"] + ".archives") / (task["id"] + ".zip")
        self.assertEqual(Path(task["artifacts"][0]["path"]), archive)
        self.assertTrue(archive.is_file())
        old = await self.manager.submit(self.route, fixtures.MAGNET, "qb")
        old["delivery"].pop("delete_after_send")
        self.manager.delivery["delete_after_send"] = True
        await self.manager._run(old)
        self.assertTrue((self.fake.root / old["id"]).is_dir())
        self.assertEqual(self.fake.cancels, [])

    async def test_manual_send_never_inherits_delete_setting(self):
        task = await self.submit_run({"auto_send": False})
        self.assertFalse(task["delivery"]["delete_after_send"])
        self.assertEqual(task["state"], "ready")
        await self.manager.resend(task, ready_only=True)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertTrue((self.fake.root / task["id"]).is_dir())
        self.assertEqual(self.fake.cancels, [])

    async def test_old_ready_archive_still_sends_from_plugin_data(self):
        task = await self.submit_run({"auto_send": False})
        current = Path(task["artifacts"][0]["path"])
        legacy = self.manager.root / "archives" / current.name
        legacy.parent.mkdir()
        current.replace(legacy)
        task.pop("artifacts")  # Earliest records stored no explicit archive path.
        self.manager.store.save(task)
        await self.manager.resend(task, ready_only=True)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertEqual(self.uploads, [("groupA", task["id"])])
        self.assertTrue(legacy.is_file())

    async def test_unknown_upload_preserves_files(self):
        self.manager.upload = AsyncMock(side_effect=TimeoutError())
        task = await self.submit_run()
        self.assertEqual(task["state"], "upload_unknown")
        self.assertFalse(task.get("delivered", False))
        self.assertTrue((self.fake.root / task["id"]).is_dir())
        self.assertTrue(Path(task["artifacts"][0]["path"]).is_file())
        self.assertEqual(self.fake.cancels, [])

    async def test_cleanup_failure_retry_never_reuploads_or_repeats_remote_delete(self):
        with patch(
            "astrbot_plugin_magnet_delivery.core.manager.cleanup_task",
            side_effect=PermissionError(),
        ):
            task = await self.submit_run()
        self.assertEqual(task["state"], "sent_cleanup_failed")
        self.assertTrue(task["delivered"])
        self.assertTrue((self.fake.root / task["id"]).exists())
        with self.assertRaises(ValueError):
            await self.manager.resend(task)
        await self.manager.retry_sent_cleanup(task)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.assertFalse((self.fake.root / task["id"]).exists())
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.fake.cancels, [task["id"]])

    async def test_remote_removal_failure_keeps_files_and_allows_cleanup_only_retry(self):
        original = self.fake.cancel
        self.fake.cancel = AsyncMock(side_effect=OSError())
        task = await self.submit_run()
        self.assertEqual(task["state"], "sent_cleanup_failed")
        self.assertTrue((self.fake.root / task["id"]).exists())
        self.fake.cancel = original
        await self.manager.retry_sent_cleanup(task)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.assertEqual(len(self.uploads), 1)

    async def test_restart_resumes_partial_cleanup_without_artifact_validation_or_upload(self):
        with patch(
            "astrbot_plugin_magnet_delivery.core.manager.cleanup_task",
            side_effect=PermissionError(),
        ):
            task = await self.submit_run()
        task["state"] = "sent_cleaning"
        self.manager.store.save(task)
        Path(task["artifacts"][0]["path"]).unlink()
        self.manager.upload = AsyncMock(side_effect=AssertionError("must not resend"))
        self.manager.start()
        saved = await self.wait_completed(task)
        self.assertTrue(saved["cleaned"])
        self.manager.upload.assert_not_awaited()

    async def test_restart_uses_persisted_success_cursor_without_duplicate_upload(self):
        task = await self.submit_run({"auto_send": False})
        task.update(state="uploading", sent_count=len(task["artifacts"]))
        task["delivery"].update(auto_send=True, delete_after_send=True)
        self.manager.store.save(task)
        self.manager.upload = AsyncMock(side_effect=AssertionError("must not resend"))
        self.manager.start()
        saved = await self.wait_completed(task)
        self.assertTrue(saved["delivered"])
        self.assertTrue(saved["cleaned"])
        self.manager.upload.assert_not_awaited()

    async def test_unconfirmed_send_cannot_trigger_cleanup(self):
        task = await self.submit_run({"auto_send": False})
        task["state"] = "sent_cleaning"
        task["delivery"].update(auto_send=True, delete_after_send=True)
        await self.manager._run(task)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "upload_unknown")
        self.assertTrue((self.fake.root / task["id"]).exists())
        self.assertEqual(self.fake.cancels, [])

    async def test_raw_batch_waits_for_every_success_and_only_then_cleans(self):
        original_start = self.fake.start

        async def start(task):
            result = await original_start(task)
            (self.fake.root / task["id"] / "second.txt").write_bytes(b"two")
            return result

        async def poll(task):
            directory = self.fake.root / task["id"]
            return Snapshot(
                total=14,
                done=14,
                complete=True,
                files=[(str(directory / "hello.txt"), 11), (str(directory / "second.txt"), 3)],
            )

        attempts = []

        async def upload(task, path):
            attempts.append(path.name)
            self.assertTrue((path.parent / "hello.txt").exists())
            self.assertTrue((path.parent / "second.txt").exists())
            if len(attempts) == 2:
                raise TimeoutError()

        self.fake.start, self.fake.poll, self.manager.upload = start, poll, upload
        task = await self.submit_run({"compress": False})
        self.assertEqual(task["state"], "upload_unknown")
        self.assertEqual(self.fake.cancels, [])
        self.assertTrue((self.fake.root / task["id"]).exists())
        await self.manager.resend(task)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertEqual(attempts, ["hello.txt", "second.txt", "second.txt"])
        self.assertTrue(self.manager.store.get(task["id"])["cleaned"])
        self.assertFalse((self.fake.root / task["id"]).exists())
