"""Persistent, bounded jobs. A download and each delivery belong to one chat."""

import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path

from .archive_worker import validated_files
from .archiver import create_archive
from .backends import BACKENDS
from .cleanup import cleanup_task, task_directory
from .delivery import (
    DEFAULTS,
    DeliveryError,
    DownloadNotSubmitted,
    configuration,
    snapshot,
    task_password,
)
from .models import ACTIVE, inside, normalize_magnet
from .store import Store

CANCELLABLE = (
    *[state for state in ACTIVE if state not in ("uploading", "sent_cleaning")],
    "ready",
    "failed",
    "cancel_failed",
    "cleanup_failed",
)


def identity(config: dict) -> str:
    values = {k: config.get(k, "") for k in ("kind", "url", "local_root", "remote_root")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def matching_profile(profile: dict, fingerprint: str) -> dict | None:
    if identity(profile) == fingerprint:
        return profile
    for directory in profile.get("legacy_local_roots", []):
        previous = dict(profile, local_root=directory)
        if identity(previous) == fingerprint:
            return previous
    return None


class Manager:
    """Drive persisted jobs from submission to verified delivery and optional cleanup."""

    def __init__(self, root: Path, profiles: dict, options: dict, notify, upload):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(root / "tasks.sqlite3")
        self.options, self.notify, self.upload = options, notify, upload
        self.delivery = configuration(options.get("delivery"))
        self.profiles = profiles
        self.backends = {name: BACKENDS[p["kind"]](p) for name, p in profiles.items()}
        self.recovery_profiles = {}
        self.legacy_backends = {}
        self.running: dict[str, asyncio.Task] = {}
        self.closing = False
        self.scheduler = None
        self.submit_lock = asyncio.Lock()
        self.cleaning: set[asyncio.Task] = set()
        self.notifications: set[asyncio.Task] = set()

    def start(self):
        # Never blindly resend a possibly delivered file after a crash.
        for task in self.store.list(active=True):
            if task["state"] == "uploading":
                # Each persisted cursor advancement records an accepted upload response.
                if task.get("artifacts") and task.get("sent_count", 0) == len(task["artifacts"]):
                    task.update(
                        delivered=True, sent_at=task.get("sent_at", time.time()), error="", speed=0
                    )
                    task["state"] = (
                        "sent_cleaning" if self._should_clean_sent(task) else "completed"
                    )
                else:
                    task.update(
                        state="upload_unknown", error="上次上传结果未知，请核对已发送的文件后使用 resend"
                    )
                self.store.save(task)
        self.scheduler = asyncio.create_task(self._schedule())

    async def submit(
        self, route: dict, magnet: str, backend: str, overrides: dict | None = None
    ) -> dict:
        async with self.submit_lock:
            if backend not in self.backends:
                raise ValueError("此下载后端未配置或未启用")
            normalized, infohash = normalize_magnet(magnet)
            active = self.store.list(active=True)
            if len(active) >= self.options["max_queue"]:
                raise ValueError("全局任务队列已满")
            if sum(t["chat"] == route["chat"] for t in active) >= self.options["max_per_chat"]:
                raise ValueError("当前聊天的进行中任务已达上限")
            # A downloader may reuse the same task for two submissions with the same hash.
            # Reserve it until the first task finishes or its cancellation is confirmed.
            uncertain = [
                t
                for t in self.store.list()
                if t["state"] == "cancel_failed"
                or (t["state"] == "sent_cleanup_failed" and not t.get("remote_removed"))
            ]
            if any(
                t["backend"] == backend and t["infohash"] == infohash for t in active + uncertain
            ):
                raise ValueError("此后端已有相同资源正在处理，请稍后再试")
            return self.store.add(
                dict(
                    route,
                    magnet=normalized,
                    infohash=infohash,
                    backend=backend,
                    fingerprint=identity(self.profiles[backend]),
                    download_root=str(self.backends[backend].root),
                    delivery=snapshot(self.delivery, self.store.settings(route["chat"]), overrides),
                )
            )

    def get_backend(self, task: dict):
        name = task["backend"]
        key = (name, task["fingerprint"])
        profile = self.profiles.get(name)
        if profile and name in self.backends and identity(profile) == key[1]:
            return self.backends[name]
        previous = self.recovery_profiles.get(key)
        if previous is None and profile:
            previous = matching_profile(profile, key[1])
        if previous is not None:
            if key not in self.legacy_backends:
                self.legacy_backends[key] = BACKENDS[previous["kind"]](previous)
            return self.legacy_backends[key]
        raise ValueError("后端已禁用或地址/目录已改变；请恢复原配置再管理此任务")

    async def cancel(self, task: dict):
        current = self.store.get(task["id"])
        if current["state"] == "uploading":
            raise ValueError("文件已进入上传阶段，无法可靠撤回；请等待结果")
        if current["state"] == "cancelling" and current["id"] in self.running:
            return
        if current["state"] not in CANCELLABLE:
            raise ValueError("此状态不能取消清理；上传结果未知时请先核对已发送的文件")
        current.update(cancel=True, state="cancelling")
        self.store.save(current)
        job = self.running.get(current["id"])
        if job and not job.done() and not job.cancelling():
            job.cancel()
        cleanup = asyncio.create_task(self._cancel_job(current, job))
        self.running[current["id"]] = cleanup
        self.cleaning.add(cleanup)
        cleanup.add_done_callback(self.cleaning.discard)

    async def _cancel_job(self, task: dict, job: asyncio.Task | None):
        if job and not job.done() and not job.cancelling():
            job.cancel()
        if job:
            await asyncio.gather(job, return_exceptions=True)
        await self._cancel(self.store.get(task["id"]))
        current = self.store.get(task["id"])
        await self._message(
            current,
            f"任务 {task['id'][:8]}："
            + (
                "已取消，下载文件及压缩临时文件已清理。"
                if current["state"] == "cancelled"
                else current["error"]
            ),
        )

    async def _cancel(self, task: dict):
        await self._purge(task, after_send=False)

    @staticmethod
    def _should_clean_sent(task: dict) -> bool:
        # Only new submissions get the new default. Older records require an explicit opt-in.
        delivery = task.get("delivery", {})
        return bool(
            delivery.get("auto_send")
            and delivery.get("delete_after_send")
            and not task.get("manual_send")
        )

    async def retry_sent_cleanup(self, task: dict):
        task = self.store.get(task["id"])
        if task["state"] != "sent_cleanup_failed" or not task.get("delivered"):
            raise ValueError("此操作仅用于已发送成功但清理失败的任务")
        job = self.running.get(task["id"])
        if job and not job.done():
            raise ValueError("清理正在进行，请稍后查看状态")
        task.update(state="sent_cleaning", error="")
        self.store.save(task)

    async def _cleanup_after_send(self, task: dict):
        if not task.get("delivered"):
            task.update(
                state="upload_unknown",
                error="未确认发送成功，已保留本地文件；请先核对已发送的文件",
                speed=0,
            )
            self.store.save(task)
            return
        if not self._should_clean_sent(task):
            # A hidden/stale setting or a manual send must never trigger deletion.
            task.update(state="completed", error="", speed=0)
            self.store.save(task)
            return
        await self._purge(task, after_send=True)

    async def _purge(self, task: dict, *, after_send: bool):
        try:
            if not task.get("remote_removed"):
                if task.get("submitted"):
                    backend = self.get_backend(task)
                    if not task.get("download_root"):
                        task["download_root"] = str(backend.root)
                    async with asyncio.timeout(75):
                        await backend.cancel(task)
                task["remote_removed"] = True
                self.store.save(task)
        except Exception:
            task.update(
                state="sent_cleanup_failed" if after_send else "cancel_failed",
                error=(
                    "文件已发送成功，但下载器任务移除失败；恢复连接后重试清理，不要重发文件"
                    if after_send
                    else "未确认下载器任务已移除，尚未清理文件；请恢复连接后重试取消"
                ),
                speed=0,
            )
            self.store.save(task)
            return
        try:
            directory = task.get("download_root")
            if not directory and task.get("submitted"):
                raise ValueError("缺少下载目录")
            worker = asyncio.create_task(
                asyncio.to_thread(
                    cleanup_task, self.root, Path(directory) if directory else None, task["id"]
                )
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A reload must wait for filesystem work before closing the store or restarting jobs.
                await worker
                raise
            task.update(
                state="completed" if after_send else "cancelled",
                error="",
                speed=0,
                cleaned=True,
                cleanup_reason="after_send" if after_send else "cancel",
                completed_at=time.time(),
            )
            for key in (
                "magnet",
                "files",
                "artifacts",
                "local_dir",
                "download_root",
                "remote_id",
                "remote_ids",
            ):
                task.pop(key, None)
            task["infohash"] = ""
            if "delivery" in task:
                task["delivery"].pop("password_tag", None)
        except Exception:
            task.update(
                state="sent_cleanup_failed" if after_send else "cleanup_failed",
                error=(
                    "文件已发送成功，但本地清理未完成；检查目录权限或文件占用后重试清理，不要重发文件"
                    if after_send
                    else "下载器任务已移除，但本地文件清理未完成；请检查目录权限或文件占用后重试清理"
                ),
                speed=0,
            )
        self.store.save(task)

    async def _start_remote(self, task: dict, backend):
        # Settle an in-flight add before cancellation verifies absence, avoiding a late orphan job.
        add = asyncio.create_task(backend.start(task))
        try:
            return await asyncio.shield(add)
        except asyncio.CancelledError:
            try:
                remote_id = await add
                current = self.store.get(task["id"])
                current["remote_id"] = remote_id
                self.store.save(current)
            except DownloadNotSubmitted:
                current = self.store.get(task["id"])
                current["submitted"] = task.get("recovering", False)
                self.store.save(current)
            except Exception:
                pass  # Cancel reconciles by backend task identity.
            raise

    async def resend(self, task: dict, *, ready_only=False):
        task = self.store.get(task["id"])
        states = ("ready",) if ready_only else ("upload_unknown", "upload_failed")
        if task["state"] not in states:
            raise ValueError("send 用于本机已就绪任务；resend 用于核对后的失败/未知上传")
        if task["id"] in self.running:
            raise ValueError("任务正在处理")
        self._artifacts(task)
        # Persist immediately, so two commands cannot enqueue duplicate resends.
        task.update(state="queued", resend=True, error="")
        if ready_only:
            task["manual_send"] = True
        self.store.save(task)

    async def _schedule(self):
        while not self.closing:
            for key, job in list(self.running.items()):
                if job.done():
                    self.running.pop(key)
                    if not job.cancelled():
                        job.exception()  # consume any unexpected exception
            for task in self.store.list(active=True):
                if not (task.get("resend") or task.get("cancel") or task["state"] == "sent_cleaning"):
                    try:
                        self.get_backend(task)
                    except ValueError:
                        continue  # Wait for the original downloader configuration.
                if len(self.running) >= self.options["concurrency"]:
                    break
                if task["id"] not in self.running:
                    self.running[task["id"]] = asyncio.create_task(self._run(task))
            await asyncio.sleep(1)

    async def _message(self, task: dict, text: str):
        try:
            await self.notify(task, text)
        except Exception:
            # A notification failure must not turn a successful delivery into a retry.
            pass

    async def _run(self, task: dict):
        backend = None
        try:
            if task["state"] == "sent_cleaning":
                await self._cleanup_after_send(task)
                await self._message(
                    task,
                    f"任务 {task['id'][:8]}："
                    + (
                        task["error"]
                        or (
                            "文件已发送，本地文件已清理。"
                            if task.get("cleaned")
                            else "文件已发送，本地文件已保留。"
                        )
                    ),
                )
                return
            if task.get("cancel"):
                await self._cancel(task)
                return
            if not task.get("resend"):
                backend = self.get_backend(task)
                if task["state"] in ("queued", "starting"):
                    previously_submitted = task.get("submitted", False)
                    task.update(state="starting", submitted=True)
                    self.store.save(task)  # write before the network mutation
                    try:
                        starting = dict(task, recovering=previously_submitted)
                        task["remote_id"] = await self._start_remote(starting, backend)
                    except DownloadNotSubmitted:
                        # The add stopped before the downloader could create a task.
                        task["submitted"] = previously_submitted
                        raise
                    task["state"] = "downloading"
                    self.store.save(task)
                if task["state"] == "downloading":
                    await self._download(task, backend)
                if task["state"] == "preparing":
                    task["state"] = (
                        "compressing" if task.get("delivery", DEFAULTS)["compress"] else "preparing"
                    )
                    self.store.save(task)
                if task["state"] == "compressing":
                    task["artifacts"] = await self._archive(task)
                else:
                    task["artifacts"] = [
                        dict(path=path, size=length) for path, length in task["files"]
                    ]
                if not task.get("delivery", DEFAULTS)["auto_send"]:
                    task.update(state="ready", error="", speed=0, completed_at=time.time())
                    self.store.save(task)
                    await self._message(
                        task,
                        f"任务 {task['id'][:8]}：文件已就绪，已保存在本机，未自动发送。"
                        f"使用 /magnet send {task['id'][:8]} 发送到原会话。",
                    )
                    return
            artifacts = self._artifacts(task)
            task["state"] = "uploading"
            self.store.save(task)
            try:
                for item in artifacts[task.get("sent_count", 0) :]:
                    await asyncio.wait_for(
                        self.upload(task, Path(item["path"])), self.options["upload_timeout"]
                    )
                    task["sent_count"] = task.get("sent_count", 0) + 1
                    self.store.save(task)
            except TimeoutError:
                task.update(
                    state="upload_unknown",
                    error="上传超时，结果未知；请先核对已发送的文件，再使用 resend",
                )
            except Exception as exc:
                task.update(
                    state="upload_unknown",
                    error=(
                        str(exc)
                        if isinstance(exc, DeliveryError)
                        else "未确认上传结果；请先核对已发送的文件，再使用 resend"
                    ),
                )
            else:
                task.update(
                    state="sent_cleaning" if self._should_clean_sent(task) else "completed",
                    delivered=True,
                    sent_at=time.time(),
                    error="",
                    speed=0,
                    completed_at=time.time(),
                )
            task.pop("resend", None)
            self.store.save(task)
            if task["state"] == "sent_cleaning":
                await self._cleanup_after_send(task)
            volume_hint = ""
            if (
                task.get("delivered")
                and len(artifacts) > 1
                and task.get("delivery", DEFAULTS)["compress"]
            ):
                volume_hint = "请将全部分卷下载到同一目录，用支持分卷的压缩软件打开 .zip.001 解压。"
            await self._message(
                task,
                f"任务 {task['id'][:8]}：{task['state']}"
                + (
                    "。" + task["error"]
                    if task["error"]
                    else "，文件已发送，本地文件已清理。"
                    if task.get("cleaned")
                    else "，文件已交给 QQ 文件接口。"
                )
                + volume_hint,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Remote exceptions may contain tokens, URLs, or private filenames.
            phase = task["state"]
            stopped = True
            if backend and task.get("submitted"):
                try:
                    async with asyncio.timeout(75):
                        await backend.abort(task)
                except Exception:
                    stopped = False
            task.update(
                state="failed" if stopped else "cancel_failed",
                error=(
                    str(exc)
                    if isinstance(exc, DeliveryError)
                    else f"{phase} 阶段失败，请检查后端连接、磁盘、大小上限和文件映射。"
                )
                + ("" if stopped else " 下载器未确认停止，请使用 cancel 重试。"),
                speed=0,
            )
            self.store.save(task)
            await self._message(task, f"任务 {task['id'][:8]}：{task['error']}")

    async def _download(self, task: dict, backend):
        failures = 0
        alert_minutes = self.options.get("zero_progress_minutes", 10)
        while True:
            if time.time() - task["created"] > self.options["task_timeout"]:
                raise TimeoutError("任务总时限")
            try:
                status = await backend.poll(task)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    raise
                await asyncio.sleep(self.options["poll_seconds"])
                continue
            if status.remote_id:
                task["remote_ids"] = list(
                    dict.fromkeys(
                        [*task.get("remote_ids", []), task["remote_id"], status.remote_id]
                    )
                )
                task["remote_id"] = status.remote_id
                self.store.save(task)
                continue
            known_total = 0 if status.total_is_estimate else status.total
            if max(known_total, status.done) > self.options["max_bytes"]:
                raise ValueError("资源超过大小限制")
            if len(status.files) > self.options["max_files"]:
                raise ValueError("资源文件数超过限制")
            local_dir = backend.root / task["id"]
            local_dir.mkdir(parents=True, exist_ok=True)
            # Keep reserve space even while metadata is unavailable.
            remaining = max(0, known_total - status.done)
            if shutil.disk_usage(local_dir).free < remaining + self.options["reserve_bytes"]:
                raise ValueError("磁盘空间不足")
            task.update(total=status.total, done=status.done, speed=status.speed)
            if status.name:
                task["name"] = status.name[:500]
            if status.complete:
                await backend.stop(task)  # stop seeding before reading files
                files = []
                for path, length in status.files:
                    resolved = inside(local_dir, Path(path))
                    if not resolved.is_file() or resolved.stat().st_size != length:
                        raise ValueError("下载文件缺失或长度不匹配")
                    files.append((str(resolved), length))
                if not files:
                    raise ValueError("下载器没有返回文件")
                task.update(
                    state="preparing", files=files, local_dir=str(local_dir.resolve()), speed=0
                )
                self.store.save(task)
                return

            alert = False
            if alert_minutes and not task.get("zero_progress_notified"):
                if status.done > 0:
                    task.pop("zero_progress_since", None)
                else:
                    now = time.time()
                    since = task.setdefault("zero_progress_since", now)
                    alert = now - since >= alert_minutes * 60
                    if alert:
                        # Persist before sending to avoid duplicate reminders after a reload.
                        task["zero_progress_notified"] = True
            self.store.save(task)
            if alert:
                reminder = asyncio.create_task(self._message(
                    task,
                    f"任务 {task['id'][:8]}：已连续约 {alert_minutes} 分钟没有下载到数据，"
                    f"任务仍在继续。可用 /magnet status {task['id'][:8]} 查看进度。",
                ))
                self.notifications.add(reminder)
                reminder.add_done_callback(self.notifications.discard)
            await asyncio.sleep(self.options["poll_seconds"])

    async def _archive(self, task: dict):
        folder = task_directory(Path(task["download_root"]), task["id"], archive=True)
        folder.mkdir(exist_ok=True)
        if shutil.disk_usage(folder).free < task["total"] + self.options["reserve_bytes"]:
            raise ValueError("没有足够空间创建 ZIP")
        # Allow ZIP headers and deflate overhead above the source size limit.
        limit = self.options["max_bytes"] * 101 // 100 + self.options["max_files"] * 1024 + 1024 * 1024
        paths = await create_archive(
            dict(
                root=task["local_dir"],
                files=task["files"],
                output=str(folder / (task["id"] + ".zip")),
                max_bytes=self.options["max_bytes"],
                upload_bytes=limit,
                volume_bytes=self.options["upload_bytes"],
                reserve_bytes=self.options["reserve_bytes"],
            ),
            self.delivery,
            task_password(task, self.delivery),
            self.options["archive_timeout"],
        )
        return [dict(path=str(path), size=path.stat().st_size) for path in paths]

    def _artifacts(self, task: dict) -> list[dict]:
        """Check the entire batch before sending any file; use persisted completed-file cursor."""
        items = task.get("artifacts")
        compressed = task.get("delivery", DEFAULTS)["compress"]
        if compressed:
            name = task["id"] + ".zip"
            legacy = self.root / "archives" / name
            if not items:
                if not legacy.is_file():
                    raise DeliveryError("压缩包已不存在")
                items = [dict(path=str(legacy), size=legacy.stat().st_size)]
            paths = [Path(item["path"]) for item in items]

            def matches(archive: Path) -> bool:
                return paths == [archive] or (
                    len(paths) > 1
                    and paths == [archive.with_name(f"{name}.{i:03d}") for i in range(1, len(paths) + 1)]
                )

            archive = legacy
            if not matches(legacy) and task.get("download_root"):
                archive = task_directory(Path(task["download_root"]), task["id"], archive=True) / name
            if not matches(archive):
                raise DeliveryError("压缩包记录与任务不符")
            for path in paths:
                inside(archive.parent, path)
        else:
            if not items or [(item["path"], item["size"]) for item in items] != [
                tuple(item) for item in task["files"]
            ]:
                raise DeliveryError("原文件记录与任务不符")
            validated_files(
                dict(
                    root=task["local_dir"], files=task["files"], max_bytes=self.options["max_bytes"]
                )
            )
        for item in items:
            path = Path(item["path"])
            if not path.is_file() or path.stat().st_size != item["size"]:
                raise DeliveryError("待发送文件缺失或大小已改变；原任务未重新下载")
            if item["size"] > self.options["upload_bytes"]:
                if compressed:
                    raise DeliveryError("压缩包超过当前文件上传上限，文件已保留")
                raise DeliveryError("原文件超过文件上传上限，请开启压缩后重新提交")
        task["artifacts"] = items
        return items

    async def close(self):
        self.closing = True
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        for job in self.running.values():
            if job not in self.cleaning:
                job.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
        for notice in self.notifications:
            notice.cancel()
        await asyncio.gather(*self.notifications, return_exceptions=True)
        for backend in [*self.backends.values(), *self.legacy_backends.values()]:
            await backend.close()
        self.store.close()
