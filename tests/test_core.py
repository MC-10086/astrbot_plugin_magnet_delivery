"""Behavioral tests with mocked HTTP plus actual ZIP subprocess execution."""

import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from astrbot_plugin_magnet_delivery.core.archive_worker import build
from astrbot_plugin_magnet_delivery.core.backends import Aria2, QBittorrent, QBittorrentDuplicate
from astrbot_plugin_magnet_delivery.core.manager import Manager, identity
from astrbot_plugin_magnet_delivery.core.models import (
    Backend,
    Snapshot,
    chat_key,
    normalize_magnet,
)
from astrbot_plugin_magnet_delivery.core.store import Store

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


class ScopeTests(unittest.TestCase):
    def test_magnet_accepts_fullwidth_query_separator(self):
        suffix = "&tr=https://example.org/announce?source=feed"
        fullwidth = MAGNET.replace("magnet:?", "MAGNET:？", 1)
        self.assertEqual(normalize_magnet(fullwidth + suffix), normalize_magnet(MAGNET + suffix))
        with self.assertRaises(ValueError):
            normalize_magnet("magnet:？xt=urn:btih:invalid")

    def test_chat_isolation_and_owner_independent_group_scope(self):
        a = chat_key("bot1", "123", "alice")
        self.assertEqual(a, chat_key("bot1", "123", "bob"))
        self.assertNotEqual(a, chat_key("bot2", "123", "alice"))
        self.assertNotEqual(a, chat_key("bot1", "456", "alice"))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            task = store.add(dict(chat=a, owner="alice", backend="qb", infohash="x"))
            with self.assertRaises(ValueError):
                store.resolve(chat_key("bot1", "456", "alice"), task["id"])
            self.assertEqual(store.resolve(a, task["id"][:8])["owner"], "alice")
            store.close()

    def test_hash_normalization_and_no_web_seed_passthrough(self):
        normalized, key = normalize_magnet(MAGNET + "&ws=http://localhost/private")
        self.assertNotIn("localhost", normalized)
        self.assertEqual(key, "a" * 40)
        import base64

        other, key2 = normalize_magnet(
            "magnet:?xt=urn:btih:" + base64.b32encode(bytes.fromhex(key)).decode()
        )
        self.assertEqual(key, key2)
        self.assertEqual(normalized, other)
        with self.assertRaises(ValueError):
            normalize_magnet("https://example.com/")

    def test_zip_rejects_escape_and_incomplete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "input"
            root.mkdir()
            outside = Path(tmp) / "private.txt"
            outside.write_text("private")
            manifest = dict(
                root=str(root),
                output=str(Path(tmp) / "out.zip"),
                files=[(str(outside), 7)],
                max_bytes=10000,
                upload_bytes=10000,
            )
            with self.assertRaises(ValueError):
                build(manifest)
            payload = root / "a.txt"
            payload.write_text("hello")
            manifest["files"] = [(str(payload), 6)]
            with self.assertRaises(ValueError):
                build(manifest)
            self.assertFalse(Path(manifest["output"]).exists())


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_qb_53_login_and_add_use_new_response_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            qb = QBittorrent(dict(url="http://qb", local_root=tmp))
            await qb.http.aclose()
            logged_in = False
            task = dict(id="1" * 32, infohash="a" * 40, magnet=MAGNET)

            def respond(request):
                nonlocal logged_in
                path = request.url.path
                if path.endswith("/login"):
                    logged_in = True
                    return httpx.Response(204)
                if not logged_in:
                    return httpx.Response(403)
                if path.endswith("/info"):
                    return httpx.Response(200, json=[])
                self.assertTrue(path.endswith("/add"))
                return httpx.Response(200, json={"success_count": 1, "failure_count": 0})

            qb.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                self.assertEqual(await qb.start(task), task["infohash"])
            finally:
                await qb.close()

    async def test_qb_never_cancels_foreign_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            qb = QBittorrent(dict(url="http://qb", local_root=tmp))
            await qb.http.aclose()
            mutations = []

            def respond(request):
                if request.method == "POST":
                    mutations.append(request.url.path)
                return httpx.Response(200, json=[dict(tags="personal", save_path=tmp)])

            qb.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            task = dict(id="1" * 32, infohash="a" * 40)
            with self.assertRaises(ValueError):
                await qb.cancel(task)
            self.assertEqual(mutations, [])
            await qb.close()

    async def test_qb_5_falls_back_to_4_pause_and_deletes_files_on_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = dict(id="1" * 32, infohash="a" * 40)
            qb = QBittorrent(dict(url="http://qb", local_root=tmp))
            await qb.http.aclose()
            calls = []
            deleted = False

            def respond(request):
                nonlocal deleted
                calls.append((request.url.path, request.content.decode()))
                if request.url.path.endswith("/info"):
                    return httpx.Response(
                        200,
                        json=[]
                        if deleted
                        else [dict(tags="astrmag_" + task["id"], save_path=qb.remote_dir(task))],
                    )
                if request.url.path.endswith("/delete"):
                    deleted = True
                if request.url.path.endswith("/stop"):
                    return httpx.Response(404)
                return httpx.Response(200, text="Ok.")

            qb.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            await qb.stop(task)
            await qb.cancel(task)
            self.assertTrue(any(path.endswith("/pause") for path, _ in calls))
            self.assertTrue(
                any(path.endswith("/delete") and "deleteFiles=true" in body for path, body in calls)
            )
            await qb.close()

    async def test_aria_follows_metadata_and_cancels_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            aria = Aria2(dict(url="http://aria/jsonrpc", local_root=tmp, secret="secret"))
            await aria.http.aclose()
            parent, child = "2" * 16, "3" * 16
            task = dict(id="2" * 32, remote_id=parent)
            removed = []
            records = {
                parent: dict(
                    status="active", files=[dict(path="[METADATA]" + "a" * 40, selected="true")]
                ),
                child: dict(status="active"),
            }

            def respond(request):
                data = json.loads(request.content)
                self.assertEqual(data["params"][0], "token:secret")
                method = data["method"].removeprefix("aria2.")
                if method == "getGlobalOption":
                    return httpx.Response(200, json={"result": {}})
                if method in ("tellActive", "tellWaiting", "tellStopped"):
                    return httpx.Response(
                        200,
                        json={
                            "result": [dict(gid=key, dir=aria.remote_dir(task)) for key in records]
                            if method == "tellStopped"
                            else []
                        },
                    )
                gid = data["params"][1]
                if data["method"].endswith("forceRemove"):
                    removed.append(gid)
                    records[gid]["status"] = "removed"
                    result = gid
                elif method == "removeDownloadResult":
                    records.pop(gid, None)
                    result = "OK"
                elif gid not in records:
                    return httpx.Response(
                        200, json={"error": {"code": 1, "message": f"GID {gid} is not found"}}
                    )
                else:
                    result = dict(dir=aria.remote_dir(task), **records[gid])
                return httpx.Response(200, json={"result": result})

            aria.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            waiting = await aria.poll(task)
            self.assertFalse(waiting.complete)
            self.assertEqual((waiting.total, waiting.files, waiting.remote_id), (0, [], ""))
            records[parent].update(status="complete", followedBy=[child])
            snapshot = await aria.poll(task)
            self.assertEqual(snapshot.remote_id, child)
            await aria.cancel(task)
            self.assertEqual(removed, [child])
            self.assertEqual(records, {})
            await aria.close()


class FakeBackend(Backend):
    def __init__(self, root, complete=True):
        self.root, self.complete = root, complete
        self.stops, self.cancels = [], []

    async def start(self, task):
        folder = self.root / task["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hello.txt").write_bytes(b"hello world")
        return "remote"

    async def poll(self, task):
        return Snapshot(
            total=11,
            done=11 if self.complete else 0,
            complete=self.complete,
            files=[(str(self.root / task["id"] / "hello.txt"), 11)],
        )

    async def stop(self, task):
        self.stops.append(task["id"])

    async def cancel(self, task):
        self.cancels.append(task["id"])

    async def close(self):
        pass


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profile = dict(kind="qb", url="http://qb", local_root=str(root / "downloads"))
        options = dict(
            concurrency=2,
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
            delivery={"archiver": "builtin"},
        )
        self.messages, self.uploads = [], []

        async def notify(task, message):
            self.messages.append((task["chat"], message))

        async def upload(task, archive):
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.read("hello.txt"), b"hello world")
            self.uploads.append((task["chat"], task["id"]))

        self.manager = Manager(root / "data", {"qb": self.profile}, options, notify, upload)
        await self.manager.backends["qb"].close()
        self.fake = FakeBackend(root / "downloads")
        self.manager.backends["qb"] = self.fake
        self.route = dict(chat="groupA", owner="alice", platform="bot1", group="123", umo="umoA")

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def test_full_pipeline_real_zip_and_original_chat_delivery(self):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(self.uploads, [("groupA", task["id"])])
        self.assertEqual(self.fake.stops, [task["id"]])

    async def test_zero_progress_reminds_once_and_survives_task_reload(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        task = await self.manager.submit(self.route, MAGNET, "qb")
        task.update(state="downloading", remote_id=await self.fake.start(task))
        clock = task["created"]
        snapshots = iter([(0, 0), (590, 1), (600, 0), (1199, 0), (1200, 0), (1220, 0)])

        async def poll(current):
            nonlocal clock
            sample = next(snapshots, None)
            if sample is None:
                raise asyncio.CancelledError
            offset, done = sample
            clock = current["created"] + offset
            self.assertEqual(len(self.messages), int(offset > 1200))
            return Snapshot(total=11, done=done)

        self.fake.poll = poll
        with patch(
            "astrbot_plugin_magnet_delivery.core.manager.time",
            SimpleNamespace(time=lambda: clock),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._download(task, self.fake)
            saved = self.manager.store.get(task["id"])
            self.assertEqual(saved["state"], "downloading")
            self.assertTrue(saved["zero_progress_notified"])
            snapshots = iter([(1850, 0), (1900, 0)])
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._download(saved, self.fake)

        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0][0], self.route["chat"])
        self.assertIn("10 分钟没有下载到数据", self.messages[0][1])
        self.assertEqual(self.fake.stops, [])
        self.assertEqual(self.fake.cancels, [])

    async def test_duplicate_qb_hash_does_not_mark_fresh_task_as_pending_removal(self):
        async def duplicate(task):
            raise QBittorrentDuplicate("qBittorrent 已有相同资源")

        self.fake.start = duplicate
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertFalse(saved["submitted"])
        self.assertIn("已有相同资源", saved["error"])
        self.assertEqual(self.fake.stops, [])

    async def test_offline_downloaders_notify_original_chat_without_pending_cleanup(self):
        for name, backend_type, marker in (
            ("qb", QBittorrent, "请启动客户端"),
            ("aria2", Aria2, "请启动 aria2"),
        ):
            with self.subTest(name=name):
                requests = []

                def offline(request):
                    requests.append(request)
                    raise httpx.ConnectError("connection refused", request=request)

                profile = dict(self.profile, kind=name)
                backend = backend_type(profile)
                await backend.http.aclose()
                backend.http = httpx.AsyncClient(transport=httpx.MockTransport(offline))
                self.manager.backends[name] = backend
                self.manager.profiles[name] = profile
                task = await self.manager.submit(self.route, MAGNET, name)
                await self.manager._run(task)
                saved = self.manager.store.get(task["id"])
                self.assertEqual(saved["state"], "failed")
                self.assertFalse(saved["submitted"])
                self.assertEqual(len(requests), 1)
                chat, message = self.messages[-1]
                self.assertEqual(chat, self.route["chat"])
                self.assertIn(marker, message)
                self.assertNotIn("未确认停止", message)

    async def test_split_archive_retry_then_cleanup(self):
        self.manager.options["upload_bytes"] = 64
        self.manager.delivery["delete_after_send"] = True
        attempts = []

        async def upload(task, path):
            attempts.append(path.name)
            if len(attempts) == 2:
                raise TimeoutError()

        self.manager.upload = upload
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "upload_unknown")
        self.assertEqual(saved["sent_count"], 1)
        parts = [Path(item["path"]) for item in saved["artifacts"]]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < path.stat().st_size <= 64 for path in parts))
        await self.manager.resend(saved)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertEqual(attempts, [parts[0].name, parts[1].name, *(p.name for p in parts[1:])])
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "completed")
        self.assertTrue(saved["cleaned"])
        self.assertFalse(parts[0].parent.exists())
        self.assertFalse((self.fake.root / task["id"]).exists())
        self.assertIn(".zip.001", self.messages[-1][1])

    async def test_local_only_archive_then_manual_send_without_downloader(self):
        self.manager.delivery["auto_send"] = False
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(self.uploads, [])
        self.assertTrue(Path(saved["artifacts"][0]["path"]).is_file())
        self.manager.backends.pop("qb")
        self.manager.profiles.pop("qb")
        await self.manager.resend(saved, ready_only=True)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertEqual(self.manager.store.get(task["id"])["state"], "completed")
        self.assertEqual(len(self.uploads), 1)

    async def test_raw_files_retry_skips_confirmed_uploads(self):
        original_start = self.fake.start

        async def start(task):
            result = await original_start(task)
            (self.fake.root / task["id"] / "second.txt").write_bytes(b"two")
            return result

        async def poll(task):
            folder = self.fake.root / task["id"]
            return Snapshot(
                total=14,
                done=14,
                complete=True,
                files=[(str(folder / "hello.txt"), 11), (str(folder / "second.txt"), 3)],
            )

        attempts = []

        async def upload(task, path):
            attempts.append(path.name)
            if len(attempts) == 2:
                raise TimeoutError()
            self.assertTrue(path.is_file())

        self.fake.start, self.fake.poll = start, poll
        self.manager.upload = upload
        task = await self.manager.submit(self.route, MAGNET, "qb", {"compress": False})
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        self.assertEqual(saved["state"], "upload_unknown")
        self.assertEqual(saved["sent_count"], 1)
        self.assertFalse((self.manager.root / "archives").exists())
        await self.manager.resend(saved)
        await self.manager._run(self.manager.store.get(task["id"]))
        self.assertEqual(attempts, ["hello.txt", "second.txt", "second.txt"])
        self.assertEqual(self.manager.store.get(task["id"])["state"], "completed")

    async def test_task_preferences_frozen_and_group_settings_are_isolated(self):
        self.manager.store.set_setting("groupA", "delivery", {"auto_send": False})
        task = await self.manager.submit(self.route, MAGNET, "qb")
        self.manager.store.set_setting("groupA", "delivery", {"auto_send": True})
        self.manager.delivery["compress"] = False
        await self.manager._run(task)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "ready")
        other = await self.manager.submit(dict(self.route, chat="groupB"), MAGNET, "qb")
        self.assertTrue(other["delivery"]["auto_send"])
        self.assertFalse(other["delivery"]["compress"])

    async def test_raw_limit_checked_before_any_upload_and_manual_send_keeps_ready(self):
        self.manager.options["upload_bytes"] = 5
        task = await self.manager.submit(
            self.route, MAGNET, "qb", {"compress": False, "auto_send": False}
        )
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        with self.assertRaisesRegex(ValueError, "上传上限"):
            await self.manager.resend(saved, ready_only=True)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "ready")
        self.assertEqual(self.uploads, [])

    async def test_missing_file_rejected_before_manual_send(self):
        task = await self.manager.submit(
            self.route, MAGNET, "qb", {"compress": False, "auto_send": False}
        )
        await self.manager._run(task)
        saved = self.manager.store.get(task["id"])
        Path(saved["files"][0][0]).unlink()
        with self.assertRaises(ValueError):
            await self.manager.resend(saved, ready_only=True)
        self.assertEqual(self.uploads, [])

    async def test_cancel_queue_does_not_touch_downloader(self):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager.cancel(task)
        await self.manager.running[task["id"]]
        self.assertEqual(self.manager.store.get(task["id"])["state"], "cancelled")
        self.assertEqual(self.fake.cancels, [])

    async def test_cancel_running_stops_owned_job_and_never_uploads(self):
        self.fake.complete = False
        task = await self.manager.submit(self.route, MAGNET, "qb")
        job = asyncio.create_task(self.manager._run(task))
        self.manager.running[task["id"]] = job
        for _ in range(100):
            if self.manager.store.get(task["id"])["state"] == "downloading":
                break
            await asyncio.sleep(0.01)
        await self.manager.cancel(task)
        await self.manager.running[task["id"]]
        self.assertEqual(self.manager.store.get(task["id"])["state"], "cancelled")
        self.assertEqual(self.fake.cancels, [task["id"]])
        self.assertEqual(self.uploads, [])
        self.assertFalse((self.fake.root / task["id"]).exists())

    async def test_timeout_is_unknown_not_completed_and_no_auto_retry(self):
        async def timeout(task, path):
            raise TimeoutError

        self.manager.upload = timeout
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "upload_unknown")
        self.assertEqual(self.manager.store.list(active=True), [])
        self.assertEqual(self.uploads, [])

    async def test_limits_stop_download_without_upload(self):
        self.manager.options["max_bytes"] = 5
        task = await self.manager.submit(self.route, MAGNET, "qb")
        await self.manager._run(task)
        self.assertEqual(self.manager.store.get(task["id"])["state"], "failed")
        self.assertEqual(self.fake.stops, [task["id"]])
        self.assertEqual(self.uploads, [])

    async def test_duplicate_rejected_and_backend_config_change_rejected(self):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        with self.assertRaises(ValueError):
            await self.manager.submit(dict(self.route, chat="groupB"), MAGNET, "qb")
        self.manager.profiles["qb"] = dict(self.profile, url="http://other")
        with self.assertRaises(ValueError):
            self.manager.get_backend(task)
        self.assertNotEqual(task["fingerprint"], identity(self.manager.profiles["qb"]))

    async def test_default_directory_upgrade_recovers_old_root_without_weakening_identity_checks(
        self,
    ):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        old_root = Path(self.profile["local_root"])
        self.manager.profiles["qb"] = dict(
            self.profile,
            local_root=str(old_root.parent / "shared"),
            legacy_local_roots=[str(old_root)],
        )
        backend = self.manager.get_backend(task)
        self.assertEqual(backend.root, old_root)
        self.assertIs(self.manager.get_backend(task), backend)
        await self.fake.start(task)
        backend.poll, backend.stop = self.fake.poll, self.fake.stop
        self.manager.backends["qb"] = FakeBackend(old_root.parent / "shared")
        await self.manager._download(task, backend)
        self.assertEqual(Path(task["local_dir"]), old_root / task["id"])
        self.manager.profiles["qb"]["url"] = "http://different-service"
        with self.assertRaises(ValueError):
            self.manager.get_backend(task)

    async def test_restart_reclassifies_uploading_without_resending(self):
        task = await self.manager.submit(self.route, MAGNET, "qb")
        task["state"] = "uploading"
        self.manager.store.save(task)
        self.manager.start()
        self.assertEqual(self.manager.store.get(task["id"])["state"], "upload_unknown")
        self.assertEqual(self.uploads, [])


if __name__ == "__main__":
    unittest.main()
