"""qBittorrent and aria2 clients, plus the registry of supported downloaders."""

import asyncio
import re
import secrets
from pathlib import Path, PurePosixPath

import httpx

from .delivery import DeliveryError, DownloadNotSubmitted
from .models import Backend, Snapshot
from .xunlei_cli import XunleiCLI


class QBittorrentDuplicate(DownloadNotSubmitted):
    """The hash belongs to an existing task with a different ownership tag."""


class Aria2RPCError(DeliveryError):
    def __init__(self, payload: dict):
        message = str(payload.get("message", ""))
        super().__init__(
            "aria2 RPC 验证失败，请检查 RPC 密钥"
            if message.strip().casefold() == "unauthorized"
            else "aria2 RPC 拒绝操作，请检查任务和连接配置"
        )
        self.missing = payload.get("code") == 1 and bool(
            re.search(r"GID\s+[0-9a-fA-F]{16}\s+is not found", message)
        )


class RemoteBackend(Backend):
    connection_hint = ""

    def __init__(self, config: dict):
        self.config = config
        self.root = Path(config["local_root"]).resolve()
        self.remote_root = config.get("remote_root") or str(self.root)
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)

    def remote_dir(self, task: dict) -> str:
        return self.remote_root.rstrip("/\\") + "/" + task["id"]

    def local_file(self, task: dict, remote_path: str) -> str:
        # Compare in downloader path syntax, independent of host OS.
        base = PurePosixPath(self.remote_dir(task).replace("\\", "/"))
        path = PurePosixPath(remote_path.replace("\\", "/"))
        rel = path.relative_to(base)
        if ".." in rel.parts or not rel.parts:
            raise ValueError("下载器返回了任务目录外的文件")
        return str(self.root / task["id"] / Path(*rel.parts))

    async def close(self):
        await self.http.aclose()

    async def request(self, method: str, url: str, **kwargs):
        try:
            return await self.http.request(method, url, **kwargs)
        except httpx.ConnectError:
            raise DeliveryError(self.connection_hint) from None
        except httpx.ConnectTimeout:
            raise DeliveryError(f"连接下载器超时。{self.connection_hint}") from None


class QBittorrent(RemoteBackend):
    connection_hint = "无法连接 qBittorrent。请启动客户端，并检查 Web UI 是否启用、端口是否正确。"

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config["url"].rstrip("/") + "/api/v2/"

    async def call(self, method: str, data=None, get=False):
        async def request():
            return await self.request(
                "GET" if get else "POST",
                self.url + method,
                params=data if get else None,
                data=None if get else data,
            )

        result = await request()
        if result.status_code == 403:
            login = await self.request(
                "POST",
                self.url + "auth/login",
                data={
                    "username": self.config.get("username", "admin"),
                    "password": self.config.get("password", ""),
                },
            )
            if login.status_code in (401, 403):
                raise DeliveryError("qBittorrent 登录失败，请检查 Web UI 用户名和密码")
            login.raise_for_status()
            if login.status_code != 204 and login.text.strip() != "Ok.":
                raise DeliveryError("qBittorrent 登录失败，请检查 Web UI 用户名和密码")
            result = await request()
        result.raise_for_status()
        return result

    async def owned(self, task: dict) -> dict | None:
        result = await self.call("torrents/info", {"hashes": task["infohash"]}, get=True)
        torrents = result.json()
        if not torrents:
            return None
        torrent = torrents[0]
        tags = [s.strip() for s in torrent.get("tags", "").split(",")]
        if "astrmag_" + task["id"] not in tags:
            raise QBittorrentDuplicate("qBittorrent 已有相同资源，请先处理原任务再重新提交")
        actual = str(torrent.get("save_path", "")).replace("\\", "/").rstrip("/")
        if actual != self.remote_dir(task).replace("\\", "/").rstrip("/"):
            raise ValueError("qBittorrent 任务路径被修改，已停止插件处理")
        return torrent

    async def start(self, task: dict) -> str:
        # A previous ambiguous add may have succeeded; recover by unique tag.
        try:
            existing = await self.owned(task)
        except DownloadNotSubmitted:
            raise
        except DeliveryError as exc:
            raise DownloadNotSubmitted(str(exc)) from None
        if existing is not None:
            return task["infohash"]
        result = await self.call(
            "torrents/add",
            {
                "urls": task["magnet"],
                "savepath": self.remote_dir(task),
                "tags": "astrmag_" + task["id"],
                "autoTMM": "false",
                "contentLayout": "Original",
                "stopped": "false",
                "paused": "false",
            },
        )
        if result.text.strip() != "Ok.":
            try:
                added = result.json()
            except ValueError:
                added = None
            if not isinstance(added, dict) or added.get("success_count") != 1:
                raise DeliveryError("qBittorrent 未确认添加任务")
        return task["infohash"]

    async def poll(self, task: dict) -> Snapshot:
        torrent = await self.owned(task)
        if torrent is None:
            raise ValueError("qBittorrent 中找不到本任务")
        state = torrent.get("state", "")
        if state in ("error", "missingFiles", "unknown"):
            raise ValueError("qBittorrent 任务异常：" + state)
        files = (await self.call("torrents/files", {"hash": task["infohash"]}, get=True)).json()
        selected = [f for f in files if f.get("priority", 1) != 0]
        # This plugin downloads complete torrents; changed file selection is rejected.
        if len(selected) != len(files):
            raise ValueError("任务文件选择被外部修改")
        ready = bool(files) and all(f.get("progress", 0) == 1 for f in files)
        total = sum(int(f["size"]) for f in files) or int(torrent.get("total_size", 0))
        local = [
            (self.local_file(task, self.remote_dir(task) + "/" + f["name"]), int(f["size"]))
            for f in files
        ]
        return Snapshot(
            total=total,
            done=int(torrent.get("completed", 0)),
            speed=int(torrent.get("dlspeed", 0)),
            complete=ready and torrent.get("amount_left", 1) == 0,
            files=local,
            name=str(torrent.get("name", "")),
        )

    async def stop(self, task: dict):
        if await self.owned(task) is None:
            return
        # qBittorrent 5 uses stop; 4.x used pause.
        try:
            await self.call("torrents/stop", {"hashes": task["infohash"]})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            await self.call("torrents/pause", {"hashes": task["infohash"]})

    async def cancel(self, task: dict):
        if await self.owned(task) is not None:
            await self.call("torrents/delete", {"hashes": task["infohash"], "deleteFiles": "true"})
        for _ in range(30):
            if await self.owned(task) is None:
                return
            await asyncio.sleep(0.2)
        raise ValueError("qBittorrent 尚未确认移除任务")


class Aria2(RemoteBackend):
    connection_hint = "无法连接 aria2。请启动 aria2，并检查 RPC 是否启用、端口是否正确。"

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config["url"]
        if config.get("managed"):
            self.connection_hint = "无法连接插件托管的 aria2。请检查程序路径并重载插件。"

    async def rpc(self, method: str, *args):
        params = ["token:" + self.config.get("secret", ""), *args]
        response = await self.request(
            "POST",
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": secrets.token_hex(8),
                "method": "aria2." + method,
                "params": params,
            },
        )
        if response.status_code in (401, 403):
            raise DeliveryError("aria2 RPC 验证失败，请检查 RPC 密钥")
        result = response.json()
        if "error" in result:
            # Never echo remote response: it may contain URLs or credentials.
            raise Aria2RPCError(result["error"])
        response.raise_for_status()
        return result["result"]

    async def start(self, task: dict) -> str:
        gid = task["remote_id"] or task["id"][:16]
        # Deterministic GID permits recovery after an interrupted addUri.
        try:
            info = await self.rpc("tellStatus", gid)
        except Aria2RPCError as exc:
            if not exc.missing:
                raise DownloadNotSubmitted(str(exc)) from None
            info = None
        except DeliveryError as exc:
            raise DownloadNotSubmitted(str(exc)) from None
        if info is not None:
            self.check_dir(task, info)
            return gid
        return await self.rpc(
            "addUri",
            [task["magnet"]],
            {
                "dir": self.remote_dir(task),
                "gid": gid,
                "seed-time": "0",
                "follow-torrent": "true",
                "bt-save-metadata": "false",
                "allow-overwrite": "false",
                "auto-file-renaming": "false",
            },
        )

    def check_dir(self, task: dict, info: dict):
        if info.get("dir", "").replace("\\", "/").rstrip("/") != self.remote_dir(task).replace(
            "\\", "/"
        ).rstrip("/"):
            raise ValueError("aria2 任务路径不匹配")

    async def poll(self, task: dict) -> Snapshot:
        info = await self.rpc("tellStatus", task["remote_id"])
        self.check_dir(task, info)
        children = info.get("followedBy", [])
        if children:
            if len(children) != 1:
                raise ValueError("aria2 返回了多个后续任务")
            # Persist child GID before the next poll, including on restart.
            return Snapshot(remote_id=children[0])
        if info["status"] == "removed":
            raise DeliveryError("aria2 任务已被移除")
        if info["status"] == "error":
            code = str(info.get("errorCode", ""))
            if code == "12":
                raise DeliveryError("aria2 已有同一磁力任务，请先处理原任务再重试（错误码 12）")
            suffix = f"（错误码 {code}）" if re.fullmatch(r"[0-9]{1,3}", code) else ""
            raise DeliveryError("aria2 下载失败" + suffix)
        entries = info.get("files", [])
        is_content = bool(info.get("bittorrent", {}).get("info"))
        # aria2 reports a relative pseudo-file while it fetches a magnet's metadata.
        if (
            not is_content
            and len(entries) == 1
            and entries[0].get("path", "").startswith("[METADATA]")
        ):
            return Snapshot()
        if any(f.get("selected") == "false" for f in entries):
            raise ValueError("aria2 任务文件选择被外部修改")
        files = [
            (self.local_file(task, f["path"]), int(f["length"]))
            for f in entries
            if f.get("selected") == "true" and f.get("path")
        ]
        # Metadata tasks can finish before the child is visible; never archive metadata.
        return Snapshot(
            total=int(info["totalLength"]),
            done=int(info["completedLength"]),
            speed=int(info["downloadSpeed"]),
            files=files,
            complete=info["status"] == "complete" and is_content and bool(files),
            name=str(info.get("bittorrent", {}).get("info", {}).get("name", "")),
        )

    async def stop(self, task: dict):
        gid = task["remote_id"] or task["id"][:16]
        info = await self.rpc("tellStatus", gid)
        self.check_dir(task, info)
        for child in info.get("followedBy", []):
            child_task = dict(task, remote_id=child)
            await self.stop(child_task)
        if info["status"] in ("active", "waiting", "paused"):
            await self.rpc("forceRemove", gid)

    async def owned_gids(self, task: dict) -> list[str]:
        """Find this task's children even after its metadata result has been removed."""
        entries = await self.rpc("tellActive", ["gid", "dir"])
        for method in ("tellWaiting", "tellStopped"):
            for offset in range(0, 10000, 250):
                page = await self.rpc(method, offset, 250, ["gid", "dir"])
                entries.extend(page)
                if len(page) < 250:
                    break
            else:
                raise ValueError("aria2 队列过大，无法确认本任务已完全移除")
        expected = self.remote_dir(task).replace("\\", "/").rstrip("/")
        return [
            entry["gid"]
            for entry in entries
            if entry.get("dir", "").replace("\\", "/").rstrip("/") == expected
        ]

    async def abort(self, task: dict):
        # aria2's cancellation clears the queue and saved session, retaining local files.
        await self.cancel(task)

    async def cancel(self, task: dict):
        pending = list(
            dict.fromkeys([task["id"][:16], task.get("remote_id", ""), *task.get("remote_ids", [])])
        )
        pending.extend(await self.owned_gids(task))
        seen, removed = set(), []
        while pending:
            gid = pending.pop(0)
            if not gid or gid in seen:
                continue
            if not re.fullmatch(r"[a-fA-F0-9]{16}", gid) or len(seen) >= 64:
                raise ValueError("aria2 任务关联记录异常")
            seen.add(gid)
            try:
                info = await self.rpc("tellStatus", gid)
            except Aria2RPCError as exc:
                if exc.missing:
                    continue
                raise
            self.check_dir(task, info)
            pending.extend(info.get("followedBy", []))
            if info["status"] in ("active", "waiting", "paused"):
                await self.rpc("forceRemove", gid)
            for _ in range(30):
                try:
                    info = await self.rpc("tellStatus", gid)
                except Aria2RPCError as exc:
                    if exc.missing:
                        break
                    raise
                self.check_dir(task, info)
                pending.extend(info.get("followedBy", []))
                if info["status"] in ("removed", "complete", "error"):
                    break
                await asyncio.sleep(0.2)
            else:
                raise ValueError("aria2 尚未确认停止任务")
            removed.append(gid)
        # Keep discovered GIDs on the task before discarding parent results. The manager
        # saves this task even if a later RPC fails, so a subsequent cancel can retry.
        task["remote_ids"] = list(seen)
        for gid in reversed(removed):
            try:
                await self.rpc("removeDownloadResult", gid)
            except Aria2RPCError as exc:
                if not exc.missing:
                    raise
        # Confirm there are no records left, including metadata and content GIDs.
        for gid in seen:
            try:
                await self.rpc("tellStatus", gid)
            except Aria2RPCError as exc:
                if exc.missing:
                    continue
                raise
            raise ValueError("aria2 中仍有任务记录")
        if await self.owned_gids(task):
            raise ValueError("aria2 中出现了后续任务，请再次取消")
        options = await self.rpc("getGlobalOption")
        if options.get("save-session"):
            await self.rpc("saveSession")


BACKENDS = {"qb": QBittorrent, "aria2": Aria2, "xunlei_cli": XunleiCLI}
