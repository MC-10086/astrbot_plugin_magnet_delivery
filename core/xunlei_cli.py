"""Call the official Windows Xunlei CLI without involving AstrBot Agent."""

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path, PureWindowsPath

from .cleanup import checked_directory
from .delivery import DeliveryError, DownloadNotSubmitted
from .models import Backend, Snapshot, inside, normalize_magnet

MISSING = 99920002
INVALID_TASK_ID = -32004
CLIENT_NOT_RUNNING = 99910001
CLIENT_CONNECTION_TIMEOUT = 99910004
CLIENT_COMMAND_FAILED = 99910008
STARTUP_RETRY_CODES = {CLIENT_NOT_RUNNING, CLIENT_CONNECTION_TIMEOUT, CLIENT_COMMAND_FAILED}
STARTUP_TIMEOUT_SECONDS = 30
LIST_LIMIT = 1000
# Allow for timestamp rounding and small clock changes before treating a task as preexisting.
PREEXISTING_MARGIN_SECONDS = 300
CLI_RELATIVE = Path("bin/windows-x64/xunlei-cli.exe")
CLI_ERRORS = {
    -32601: "迅雷客户端版本不支持此命令，请更新客户端",
    -32006: "请先在迅雷客户端登录账号",
    -32001: "迅雷不接受当前下载目录，请检查目录配置",
    -32003: "迅雷未确认创建任务，请在客户端核对",
    INVALID_TASK_ID: "迅雷未找到有效任务编号，请在客户端核对任务状态",
    CLIENT_NOT_RUNNING: "请启动迅雷客户端",
    99910002: "请安装迅雷客户端",
    99910003: "迅雷客户端启动失败，请尝试手动启动",
    CLIENT_CONNECTION_TIMEOUT: "迅雷客户端连接超时，请检查客户端是否正在运行",
    99910007: "迅雷客户端与 CLI 协议版本不兼容",
    CLIENT_COMMAND_FAILED: "已连接迅雷客户端，但命令执行失败",
    99920001: "请先在迅雷客户端登录账号",
}


def discover_cli(data_dir: Path) -> Path | None:
    for name in ("xunlei-cli-skill", "xunlei-cli"):
        candidate = data_dir / "skills" / name / CLI_RELATIVE
        if candidate.is_file():
            return candidate
    return None


class XunleiCLIError(DeliveryError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"迅雷 CLI：{CLI_ERRORS.get(code, '命令执行失败')}（错误码 {code}）")


class XunleiCLINotSubmitted(DownloadNotSubmitted):
    """The CLI add command was never issued for this new task."""


def _task_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", value):
        raise DeliveryError("迅雷 CLI 未返回有效任务编号")
    return value


def _quantity(value) -> int:
    if type(value) not in (float, int) or not 0 <= value <= 2**63 - 1 or int(value) != value:
        raise DeliveryError("迅雷 CLI 返回了无效的下载大小")
    return int(value)


class XunleiCLI(Backend):
    def __init__(self, profile: dict):
        self.root = Path(profile["local_root"])
        self.executable = profile.get("executable", "")
        self.max_files = profile.get("max_files", 2000)
        self._startup_lock = asyncio.Lock()

    async def command(self, *args: str, app: bool = False):
        if os.name != "nt":
            raise DeliveryError("迅雷官方 CLI 后端需要在 Windows 上运行 AstrBot")
        path = Path(self.executable)
        if not path.is_absolute() or path.name.lower() != "xunlei-cli.exe" or not path.is_file():
            raise DeliveryError("未找到迅雷 CLI，请在插件设置中选择 xunlei-cli.exe")
        try:
            env = os.environ.copy()
            env.pop("XUNLEI_CLI_LAUNCH", None)
            proc = await asyncio.create_subprocess_exec(
                str(path), "app" if app else "task", *args, "--format", "json",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, env=env,
            )
        except OSError:
            raise DeliveryError("无法运行迅雷 CLI，请检查程序路径与权限") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DeliveryError("迅雷 CLI 命令响应超时，请检查迅雷客户端") from None
        try:
            result = json.loads(stderr if proc.returncode else stdout)
        except (ValueError, UnicodeError):
            raise DeliveryError("迅雷 CLI 响应格式异常，请检查客户端和 CLI 版本") from None
        if isinstance(result, dict):
            if not proc.returncode and result.get("ok") is True:
                return result.get("data")
            error = result.get("error")
            if (
                result.get("ok") is False
                and isinstance(error, dict)
                and type(error.get("code")) is int
            ):
                raise XunleiCLIError(error["code"])
        raise DeliveryError("迅雷 CLI 响应格式异常，请检查客户端和 CLI 版本")

    async def health(self) -> str:
        try:
            await self.list_tasks(1)
        except DeliveryError as exc:
            return str(exc)
        return "迅雷 CLI 连接正常（未创建下载任务）"

    async def list_tasks(self, limit: int) -> list:
        rows = await self.command(
            "list", "--status", "all", "--limit", str(limit), "--sort-by", "time", "--order", "desc"
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DeliveryError("迅雷 CLI 未返回有效任务列表")
        return rows

    async def ensure_client_running(self):
        # Check inside the lock so concurrent submissions see the client started by the first one.
        async with self._startup_lock:
            try:
                await self.list_tasks(1)
            except XunleiCLIError as exc:
                if exc.code not in STARTUP_RETRY_CODES:
                    raise
                launch_needed = exc.code == CLIENT_NOT_RUNNING
            else:
                return
            try:
                async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
                    if launch_needed:
                        try:
                            await self.command("launch", "--wait", "10", app=True)
                        except XunleiCLIError as exc:
                            if exc.code not in STARTUP_RETRY_CODES:
                                raise
                    # A running process can report a transient error before its task API is ready.
                    while True:
                        try:
                            await self.list_tasks(1)
                            return
                        except XunleiCLIError as exc:
                            if exc.code not in STARTUP_RETRY_CODES:
                                raise
                        await asyncio.sleep(1)
            except TimeoutError:
                raise DeliveryError("等待迅雷下载服务就绪超时，请检查客户端后重试") from None

    def folder(self, task: dict) -> Path:
        return checked_directory(self.root / task["id"])

    def matches(self, folder: Path, row: dict, task: dict) -> bool:
        value = row.get("save_dir")
        if not isinstance(value, str) or not Path(value).is_absolute() or Path(value) != folder:
            return False
        return self.matches_source(row, task)

    @staticmethod
    def matches_source(row: dict, task: dict) -> bool:
        url = row.get("url")
        if not isinstance(url, str):
            return False
        try:
            return normalize_magnet(url)[1] == task["infohash"]
        except ValueError:
            # The Windows client replaces the magnet with its cached BT descriptor.
            source = PureWindowsPath(url)
            cache = tuple(part.casefold() for part in source.parent.parts[-4:])
            return (
                source.is_absolute()
                and cache == ("thunder network", "xmp6", "xunlei", "torrents")
                and source.name.casefold() == task["infohash"] + ".torrent"
            )

    def is_preexisting(self, folder: Path, row: dict, task: dict) -> bool:
        # +add may return the ID of an older personal download instead of creating a task.
        # Only detach when its source, different directory, and earlier creation time agree.
        value, created = row.get("save_dir"), row.get("create_time")
        submitted_at = task.get("created")
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or not isinstance(created, str)
            or type(submitted_at) not in (int, float)
            or not self.matches_source(row, task)
        ):
            return False
        try:
            original_at = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").timestamp()
            if not original_at + PREEXISTING_MARGIN_SECONDS < submitted_at:
                return False
            other = Path(value).resolve()
            return not other.is_relative_to(folder) and not folder.is_relative_to(other)
        except (OSError, RuntimeError, ValueError):
            return False

    async def find(self, task: dict, *, missing_id: str = "") -> str | None:
        folder = self.folder(task)
        rows = await self.list_tasks(LIST_LIMIT)
        found = [row for row in rows if Path(str(row.get("save_dir", ""))) == folder]
        if len(found) == 1 and self.matches(folder, found[0], task):
            return _task_id(found[0].get("task_id"))
        if found or len(rows) >= LIST_LIMIT or (
            missing_id and any(row.get("task_id") == missing_id for row in rows)
        ):
            raise DeliveryError("无法确认迅雷任务归属，请在客户端核对后重试")
        return None

    async def start(self, task: dict) -> str:
        if task.get("recovering"):
            remote_id = task.get("remote_id") or await self.find(task)
            if not remote_id:
                raise DeliveryError("迅雷未找到上次提交的任务，请检查客户端后重新添加")
            return remote_id
        try:
            await self.ensure_client_running()
            folder = self.folder(task)
            folder.mkdir(parents=True, exist_ok=True)
            if any(folder.iterdir()):
                raise DeliveryError("迅雷任务目录已有文件，请清理任务后重新添加")
        except DeliveryError as exc:
            raise XunleiCLINotSubmitted(str(exc)) from None
        except (OSError, ValueError):
            raise XunleiCLINotSubmitted("无法创建迅雷任务目录，请检查目录权限") from None
        magnet = task["magnet"].replace("xt=urn%3Abtih%3A", "xt=urn:btih:", 1)
        params = json.dumps({"url": [magnet], "save_dir": str(folder)}, ensure_ascii=False)
        result = await self.command("+add", "--params", params)
        if not isinstance(result, dict):
            raise DeliveryError("迅雷 CLI 未返回有效任务编号")
        ids = result.get("task_ids")
        remote_id = result.get("task_id") or (
            ids[0] if isinstance(ids, list) and len(ids) == 1 else None
        )
        return _task_id(remote_id)

    async def detail(self, remote_id: str, *, task: dict | None = None) -> dict | None:
        try:
            row = await self.command("detail", "--task-id", _task_id(remote_id))
        except XunleiCLIError as exc:
            if exc.code in (MISSING, INVALID_TASK_ID) and task is not None:
                # Before treating a missing ID as gone, look for the same download
                # under a new ID. find() verifies both the directory and BT hash.
                replacement = await self.find(task, missing_id=remote_id)
                if replacement is None:
                    return None
                if replacement != remote_id:
                    row = await self.detail(replacement)
                    if row is not None:
                        return row
                raise DeliveryError("迅雷任务详情与列表不一致，请在客户端核对") from None
            if exc.code == MISSING:
                return None
            raise
        if not isinstance(row, dict) or row.get("task_id") != remote_id:
            raise DeliveryError("迅雷 CLI 返回的任务编号不一致")
        return row

    def completed_files(self, folder: Path) -> list[tuple[str, int]]:
        if not folder.is_dir():
            raise DeliveryError("迅雷已完成，但找不到任务下载目录")
        files = []
        pending = [folder]
        while pending:
            for entry in pending.pop().iterdir():
                path = inside(folder, entry)
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    files.append((str(path), path.stat().st_size))
                    if len(files) > self.max_files:
                        raise DeliveryError("迅雷资源文件数超过限制")
                else:
                    raise DeliveryError("迅雷下载目录含有异常文件")
        if not files:
            raise DeliveryError("迅雷已完成，但任务目录中没有可交付的文件")
        return sorted(files)

    async def poll(self, task: dict) -> Snapshot:
        folder = self.folder(task)
        current_id = task["remote_id"]
        row = await self.detail(current_id, task=task)
        if row is None:
            raise DeliveryError("迅雷中未找到匹配的任务，请在客户端核对")
        if not self.matches(folder, row, task):
            if row["task_id"] == current_id and self.is_preexisting(folder, row, task):
                raise DeliveryError(
                    "迅雷已有同一资源的旧任务。请检查迅雷的下载列表和回收站，处理同一资源的旧记录后再提交"
                )
            raise DeliveryError("迅雷任务链接或保存目录不一致，无法确认归属")
        if row["task_id"] != current_id:
            if row["task_id"] in task.get("remote_ids", ()):
                raise DeliveryError("迅雷反复更换任务编号，请在客户端核对")
            # Let the manager persist the new ID before querying progress again.
            return Snapshot(remote_id=row["task_id"])
        state = row.get("status")
        if state == "error" or row.get("file_deleted") or (state == "done" and row.get("file_errors")):
            raise DeliveryError("迅雷下载失败或文件不完整，请在迅雷客户端查看")
        name = row.get("name")
        name = name if isinstance(name, str) else ""
        if state == "done":
            # The torrent total can include files that Xunlei did not select.
            files = await asyncio.to_thread(self.completed_files, folder)
            size = sum(length for _, length in files)
            return Snapshot(total=size, done=size, complete=True, files=files, name=name)

        total = _quantity(row.get("total_size", 0))
        done = _quantity(row.get("downloaded_size", 0))
        speed = row.get("speed", 0)
        speed = int(speed * 1024**2) if type(speed) in (float, int) and 0 <= speed < 2**40 else 0
        return Snapshot(
            total=total, done=done, speed=speed, name=name, total_is_estimate=True,
        )

    async def stop(self, task: dict):
        # The CLI reports done only after the download has completed.
        pass

    async def abort(self, task: dict):
        await self.cancel(task)

    async def cancel(self, task: dict):
        remote_id = task.get("remote_id") or await self.find(task)
        if not remote_id:
            return
        row = await self.detail(remote_id, task=task)
        if row is None:
            return
        folder = self.folder(task)
        if not self.matches(folder, row, task):
            if row["task_id"] == remote_id and self.is_preexisting(folder, row, task):
                # This task never belonged to the plugin. Leave the client and its files alone.
                return
            raise DeliveryError("无法确认迅雷任务归属，已保留任务与本地文件")
        remote_id = row["task_id"]
        task["remote_id"] = remote_id
        try:
            await self.command("remove", "--task-id", remote_id)
        except XunleiCLIError as exc:
            if exc.code != MISSING:
                raise
        for _ in range(3):
            if await self.detail(remote_id, task=task) is None:
                return
            await asyncio.sleep(1)
        raise DeliveryError("迅雷尚未确认移除任务")
