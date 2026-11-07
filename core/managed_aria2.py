"""Own a private aria2 child; never inspect/stop unrelated aria2 processes."""

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import httpx


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ManagedAria2:
    def __init__(self, root: Path, executable: str, download_dir: Path):
        self.root = root.resolve()
        self.executable = executable.strip().strip('"')
        self.download_dir = download_dir.resolve()
        self.process = None
        self.client = None
        self.state = {}
        self.error = "尚未启动"

    def _prepare(self):
        binary = Path(self.executable) if self.executable else Path(shutil.which("aria2c") or "")
        if not binary.is_file():
            raise ValueError("未找到 aria2c，请在自动管理中填写 aria2c.exe 的完整路径")
        self.root.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        state_file = self.root / "connection.json"
        if state_file.exists():
            self.restore_connection()
        else:
            self.state = {"port": free_port(), "secret": secrets.token_hex(32)}
            with state_file.open("x", encoding="utf-8") as stream:
                os.chmod(state_file, 0o600)
                json.dump(self.state, stream)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", self.state["port"]))
            except OSError:
                raise ValueError(
                    "托管端口被占用；不会接管或停止已有进程，请先关闭之前的托管实例"
                ) from None
        session = self.root / "session.txt"
        session.touch(exist_ok=True)
        values = {
            "enable-rpc": "true",
            "rpc-listen-all": "false",
            "rpc-listen-port": str(self.state["port"]),
            "rpc-secret": self.state["secret"],
            "dir": str(self.download_dir),
            "input-file": str(session),
            "save-session": str(session),
            "save-session-interval": "10",
            "force-save": "true",
            "enable-dht": "true",
            "enable-dht6": "false",
            "dht-file-path": str(self.root / "dht.dat"),
            "listen-port": str(free_port()),
            "dht-listen-port": str(free_port(socket.SOCK_DGRAM)),
            "bt-enable-lpd": "false",
            "seed-time": "0",
            "console-log-level": "error",
        }
        conf = self.root / "aria2.conf"
        if any("\n" in value or "\r" in value for value in values.values()):
            raise ValueError("托管目录包含非法换行字符")
        with conf.open("w", encoding="utf-8") as stream:
            os.chmod(conf, 0o600)
            stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        return binary.resolve(), conf

    def restore_connection(self):
        state = json.loads((self.root / "connection.json").read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("port"), int)
            or not 1 <= state["port"] <= 65535
            or not isinstance(state.get("secret"), str)
            or len(state["secret"]) < 32
        ):
            raise ValueError("托管连接文件损坏，请检查插件数据目录")
        self.state = state

    @property
    def profile(self):
        return {
            "name": "aria2",
            "kind": "aria2",
            "managed": True,
            "url": f"http://127.0.0.1:{self.state['port']}/jsonrpc",
            "secret": self.state["secret"],
            "local_root": str(self.download_dir),
        }

    async def rpc(self, method: str):
        response = await self.client.post(
            self.profile["url"],
            json={
                "jsonrpc": "2.0",
                "id": "managed",
                "method": "aria2." + method,
                "params": ["token:" + self.state["secret"]],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError("托管 aria2 RPC 检查失败")
        return payload["result"]

    async def start(self):
        try:
            binary, conf = self._prepare()
            kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
            self.process = subprocess.Popen(
                [str(binary), "--conf-path=" + str(conf)],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            self.client = httpx.AsyncClient(timeout=2, trust_env=False)
            for _ in range(50):
                if self.process.poll() is not None:
                    raise ValueError("aria2 启动后退出，请检查程序文件和下载目录")
                try:
                    version = await self.rpc("getVersion")
                    self.error = ""
                    return version["version"]
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.2)
            raise ValueError("aria2 启动超时，请检查本机防火墙或程序权限")
        except Exception as exc:
            self.error = (
                str(exc)
                if isinstance(exc, ValueError)
                else "aria2 启动失败，请检查程序路径、目录权限和本机网络"
            )
            await self.close()
            raise ValueError(self.error) from None

    async def health(self):
        if not self.process or self.process.poll() is not None:
            return "自动管理：" + (self.error or "进程已停止，请重载插件重新启动")
        try:
            result = await self.rpc("getVersion")
            return "自动管理：运行正常，aria2 " + result["version"]
        except (httpx.HTTPError, ValueError):
            return "自动管理：进程存在，但 RPC 连接失败"

    async def close(self):
        process = self.process
        if process and process.poll() is None:
            if self.client:
                try:
                    await self.rpc("saveSession")
                    await self.rpc("shutdown")
                except (httpx.HTTPError, ValueError):
                    pass
            for _ in range(25):
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.2)
            if process.poll() is None:
                process.terminate()  # exact child handle, never a process-name kill
                try:
                    await asyncio.to_thread(process.wait, 3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        if self.client:
            await self.client.aclose()
            self.client = None
