"""AstrBot entry point; commands never wait for a download to finish."""

import asyncio
import re
from itertools import islice
from pathlib import Path
from uuid import uuid4

from aiocqhttp import CQHttp
from aiocqhttp.api_impl import HttpApi, UnifiedApi, WebSocketReverseApi
from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable, NetworkError
from aiocqhttp.exceptions import Error as OneBotError
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Forward, Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.configuration import (
    configured_profiles,
    download_directory,
    download_selection,
    migrate_preview_config,
)
from .core.delivery import (
    ARCHIVER_NAMES,
    DEFAULTS,
    DeliveryError,
    describe,
    engine,
    preferences,
    snapshot,
)
from .core.forward import collect_magnets, forwarded_texts
from .core.managed_aria2 import ManagedAria2
from .core.manager import Manager, matching_profile
from .core.models import chat_key
from .core.path_picker import PathPicker
from .core.preview import PreviewService, render
from .core.task_view import listing
from .core.xunlei_cli import XunleiCLI

HELP = """磁力下载命令 · QQ
< > 内为必填项，[ ] 内为选填项，| 表示任选其一。
任务编号可填完整编号或至少前 8 位，按当前会话和机器人实例查询。
/magnet 可写成 /磁力，preview 可写成 预览。
私聊需由管理员在 AstrBot 插件配置中启用，白名单留空时对所有 QQ 用户开放。

【帮助、下载和查询】
/help 或 /magnet-help 或 /magnet [help]　查看这份帮助
/magnet backends　查看插件选定的下载器
/magnet add <磁力链接> [选项]　用选定的下载器添加任务
单次选项（每对选一个，可组合）
  --compress / --no-compress　压缩开关
  --encrypt / --no-encrypt　密码保护开关
  --send / --no-send　自动发回当前会话开关
  --delete-after-send / --no-delete-after-send　发送后清理开关
/magnet preview <磁力链接|任务编号>　预览名称、大小和已有截图
/magnet tasks [all]　查看进行中、已就绪或需处理的任务，all 显示最近 20 条记录
/magnet status <任务编号>　查看下载进度和交付状态
/magnet delivery　查看当前会话的压缩、加密、发送及清理设置

【任务操作 · 提交者或管理员】
/magnet cancel <任务编号>　取消并清理本地文件，上传中无法可靠撤回
/magnet send <任务编号>　将本机已就绪的文件发回当前会话
/magnet resend <任务编号>　核对已发送的文件后重试未确认的上传
/magnet cleanup <任务编号>　重试发送成功后的本地清理

【本群设置 · 群管理员或机器人管理员】
/magnet test [后端名称]　测试下载器连接
/magnet on|off　启用或关闭本群新任务
/magnet auto on|off　开启或关闭本群磁力链接自动下载
/magnet compress on|off|default　设置自动压缩
/magnet encrypt on|off|default　设置密码保护，需要开启压缩并在面板配置密码
/magnet autosend on|off|default　设置是否自动发回本群
/magnet autodelete on|off|default　设置发送成功后是否清理本地文件，需要开启自动发送
default 表示使用插件全局设置。"""

STATES = {
    "queued": "排队",
    "starting": "提交中",
    "downloading": "下载中",
    "preparing": "准备文件",
    "compressing": "压缩中",
    "uploading": "上传中",
    "sent_cleaning": "已发送，清理中",
    "sent_cleanup_failed": "已发送，清理失败",
    "completed": "已交付",
    "ready": "本机已就绪（未自动发送）",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "failed": "失败",
    "cancel_failed": "停止待确认",
    "cleanup_failed": "文件清理失败",
    "upload_unknown": "上传结果未知",
    "upload_failed": "上传失败",
}


def size(value):
    return f"{int(value) / 1024 / 1024:.1f} MiB"


class MagnetDelivery(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.manager = None
        self.preview = None
        self._forward_lock = asyncio.Lock()
        self.managed_aria2 = None
        self.download_backend = "none"
        self.path_picker = PathPicker(
            config, Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_magnet_delivery"
        )
        if callable(getattr(context, "register_web_api", None)):
            for route, handler, method in (
                ("paths", self.paths_get, "GET"),
                ("paths/browse", self.paths_browse, "GET"),
                ("paths/save", self.paths_save, "POST"),
                ("paths/test-xunlei-cli", self.paths_test_xunlei_cli, "GET"),
                ("tasks", self.tasks_get, "GET"),
                ("tasks/cancel", self.tasks_cancel, "POST"),
                ("tasks/cleanup", self.tasks_cleanup, "POST"),
                ("tasks/send", self.tasks_send, "POST"),
                ("tasks/resend", self.tasks_resend, "POST"),
            ):
                context.register_web_api(
                    "/astrbot_plugin_magnet_delivery/" + route, handler, [method], "磁力任务管理"
                )

    async def tasks_get(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        if not self.manager or self.manager.closing:
            return error_response("插件尚未初始化或正在重载，请稍后刷新", status_code=503)
        return json_response(listing(self.manager), headers={"Cache-Control": "no-store"})

    async def tasks_cancel(self):
        return await self._task_action("cancel")

    async def tasks_cleanup(self):
        return await self._task_action("cleanup")

    async def tasks_send(self):
        return await self._task_action("send")

    async def tasks_resend(self):
        return await self._task_action("resend")

    async def _task_action(self, action: str):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        manager = self.manager
        if not manager or manager.closing:
            return error_response("插件正在重载，请稍后刷新", status_code=503)
        payload = await request.json(default={})
        if manager is not self.manager or manager.closing:
            return error_response("插件正在重载，请稍后刷新", status_code=503)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", payload["id"])
            or not isinstance(payload.get("chat"), str)
        ):
            return error_response("请选择有效任务", status_code=400)
        task = manager.store.get(payload["id"])
        if not task or task["chat"] != payload["chat"]:
            return error_response("当前聊天中没有此任务，请刷新列表", status_code=404)
        if action == "resend" and payload.get("confirmed") is not True:
            return error_response("请先核对 QQ 中已收到的文件", status_code=400)
        try:
            if action in ("send", "resend"):
                if not task["group"] and not self._private_allowed(task["owner"]):
                    raise ValueError("此私聊已失去授权")
                await manager.resend(task, ready_only=action == "send")
                message = "已排队发送到原会话。" if action == "send" else "已排队重发未确认的文件。"
            elif action == "cleanup":
                await manager.retry_sent_cleanup(task)
                message = "已排队重试本地清理，不会重新上传文件。"
            else:
                await manager.cancel(task)
                message = "正在停止下载并清理文件，请等待最终状态。"
            return json_response({"accepted": True, "message": message}, status_code=202)
        except ValueError as exc:
            return error_response(str(exc), status_code=409)

    async def paths_get(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        return json_response(dict(self.path_picker.settings(), download_backend=self.download_backend))

    async def paths_browse(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            return error_response("页码无效", status_code=400)
        try:
            kind = request.query.get("kind", "")
            result = await asyncio.to_thread(
                self.path_picker.browse,
                request.query.get("path", ""),
                True if kind == "executable" else kind,
                page,
            )
            return json_response(result)
        except (ValueError, OSError) as exc:
            return error_response(
                str(exc) if isinstance(exc, ValueError) else "目录不可访问", status_code=400
            )

    async def paths_save(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        try:
            payload = await request.json(default={})
            return json_response(
                self.path_picker.save(payload.get("paths") if isinstance(payload, dict) else None)
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def paths_test_xunlei_cli(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板", status_code=401)
        backend = self.manager.backends.get("xunlei_cli") if self.manager else None
        if not isinstance(backend, XunleiCLI):
            return error_response("请在插件配置启用迅雷官方 CLI 并重载插件", status_code=409)
        return json_response({"message": await backend.health()}, headers={"Cache-Control": "no-store"})

    async def initialize(self):
        try:
            await self._initialize()
        except BaseException:
            await self.terminate()
            raise

    async def _initialize(self):
        if migrate_preview_config(self.config) and hasattr(self.config, "save_config"):
            self.config.save_config()
        root = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_magnet_delivery"
        self.download_backend, advanced_name = download_selection(self.config)
        config = dict(self.config, advanced_name=advanced_name)
        profiles = configured_profiles(config, root, selected=self.download_backend)
        if self.download_backend == "aria2_auto":
            service = self._managed_service(root)
            if await self._start_managed(service):
                profiles["aria2"] = self._managed_profile(service, root)
        mib = 1024 * 1024
        options = {
            "concurrency": max(1, min(8, int(self.config.get("concurrency", 2)))),
            "max_queue": max(1, int(self.config.get("max_queue", 50))),
            "max_per_chat": max(1, int(self.config.get("max_per_chat", 5))),
            "max_bytes": max(1, int(self.config.get("max_download_mib", 8192))) * mib,
            "upload_bytes": min(4096, max(1, int(self.config.get("max_upload_mib", 3800)))) * mib,
            "max_files": max(1, int(self.config.get("max_files", 2000))),
            "reserve_bytes": max(64, int(self.config.get("reserve_mib", 512))) * mib,
            "poll_seconds": max(2, int(self.config.get("poll_seconds", 5))),
            "zero_progress_minutes": max(0, int(self.config.get("zero_progress_minutes", 10))),
            "task_timeout": max(60, int(self.config.get("task_timeout", 21600))),
            "archive_timeout": max(30, int(self.config.get("archive_timeout", 1800))),
            "upload_timeout": max(30, int(self.config.get("upload_timeout", 1800))),
            "delivery": self.config.get("delivery", {}),
        }
        self.manager = Manager(root, profiles, options, self._notify, self._upload)
        await self._restore_downloaders(root)
        if hasattr(self.config, "save_config") and (
            self.config.get("default_backend") or self.config.get("download_backend") == "xunlei"
        ):
            self.config["download_backend"] = self.download_backend
            self.config["advanced_name"] = advanced_name
            self.config["default_backend"] = ""
            self.config.save_config()
        self.manager.start()
        preview_config = self.config.get("preview", {})
        self.preview = PreviewService(
            proxy=preview_config.get("preview_proxy", "system"),
            cooldown=max(1, int(preview_config.get("preview_cooldown", 15))),
            cache_seconds=max(30, int(preview_config.get("preview_cache_seconds", 3600))),
            image_hosts=preview_config.get(
                "preview_image_hosts", ["whatslink.info", ".whatslink.info"]
            ),
        )
        logger.info("磁力插件已启动，已启用 %d 个后端；新群默认关闭", len(profiles))

    def _managed_service(self, root):
        return ManagedAria2(
            root / "managed_aria2", self.config.get("aria2_auto", {}).get("executable", ""),
            download_directory(self.config, root, "aria2_auto"),
        )

    def _managed_profile(self, service, root):
        profile = service.profile
        if not str(self.config.get("aria2_auto", {}).get("download_dir", "")).strip():
            profile["legacy_local_roots"] = [str((root / "downloads" / "aria2").resolve())]
        return profile

    async def _start_managed(self, service):
        self.managed_aria2 = service
        try:
            await service.start()
        except ValueError as exc:
            logger.warning("aria2 自动管理：%s", str(exc))
            return False
        return True

    async def _restore_downloaders(self, root):
        manager = self.manager
        candidates = {}
        for task in manager.store.list():
            if task["state"] in ("cancelled", "completed") or task.get("remote_removed"):
                continue
            try:
                manager.get_backend(task)
                continue
            except ValueError:
                pass
            name = task["backend"]
            if name not in candidates:
                profiles = []
                if name in ("qb", "aria2", "xunlei_cli"):
                    try:
                        profiles.extend(configured_profiles(
                            self.config, root, selected=name, create_dirs=False
                        ).values())
                    except (ValueError, TypeError):
                        pass
                try:
                    profiles.extend(configured_profiles(
                        dict(self.config, advanced_name=name), root,
                        selected="advanced", create_dirs=False,
                    ).values())
                except ValueError:
                    pass
                candidates[name] = profiles
            for profile in candidates[name]:
                previous = matching_profile(profile, task["fingerprint"])
                if previous:
                    manager.recovery_profiles[name, task["fingerprint"]] = previous
                    break
            else:
                if name == "aria2" and self.download_backend != "aria2_auto":
                    service = self.managed_aria2
                    if service is None:
                        try:
                            service = self._managed_service(root)
                            service.restore_connection()
                        except (OSError, ValueError):
                            continue
                    previous = matching_profile(self._managed_profile(service, root), task["fingerprint"])
                    if previous and (service.process or await self._start_managed(service)):
                        manager.recovery_profiles[name, task["fingerprint"]] = previous

    def route(self, event: AstrMessageEvent) -> dict:
        platform, group, owner = (
            str(event.get_platform_id()),
            str(event.get_group_id() or ""),
            str(event.get_sender_id()),
        )
        return {
            "platform": platform,
            "group": group,
            "owner": owner,
            "chat": chat_key(platform, group, owner),
            "umo": event.unified_msg_origin,
            "self_id": str(event.message_obj.self_id),
        }

    def _client(self, platform_id: str):
        platform = self.context.get_platform_inst(platform_id)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise ValueError("QQ OneBot 接入端不可用")
        return platform.get_client()

    async def _is_manager(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        if not event.get_group_id():
            return False
        try:
            client = self._client(str(event.get_platform_id()))
            result = await client.call_action(
                "get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=True,
            )
            return isinstance(result, dict) and result.get("role") in ("owner", "admin")
        except Exception:
            return False  # fail closed, never grant permissions on API failure

    async def _notify(self, task: dict, text: str):
        if not task["group"] and not self._private_allowed(task["owner"]):
            return
        await self.context.send_message(task["umo"], MessageChain([Plain(text)]))

    async def _upload(self, task: dict, archive: Path):
        group = task["group"]
        if not group and not self._private_allowed(task["owner"]):
            raise DeliveryError("私聊权限已关闭或取消，文件保留在本机")
        client = self._client(task["platform"])
        # Only translate a prefix, never accept a path supplied by a chat user.
        local_prefix = self.config.get("qq_local_prefix", "").strip()
        remote_prefix = self.config.get("qq_remote_prefix", "").strip()
        path = str(archive.resolve())
        if local_prefix or remote_prefix:
            if not local_prefix or not remote_prefix:
                raise ValueError("QQ 路径映射须同时填写两个前缀")
            relative = archive.resolve().relative_to(Path(local_prefix).resolve())
            path = remote_prefix.rstrip("/\\") + "/" + relative.as_posix()
        name = archive.name
        if task.get("delivery", DEFAULTS)["compress"]:
            base = f"{task['id']}.zip"
            name = f"magnet_{task['id'][:8]}.zip"
            if archive.name.startswith(base + "."):
                name += archive.name.removeprefix(base)
        action = "upload_group_file" if group else "upload_private_file"
        target = {"group_id": int(group)} if group else {"user_id": str(task["owner"])}
        args = dict(target, file=path, name=name)
        if task.get("self_id"):
            args["self_id"] = task["self_id"]
        api = client
        if isinstance(client, CQHttp) and isinstance(client._api, UnifiedApi):
            # AstrBot uses a short timeout for regular OneBot calls. Reuse its live
            # connections without changing the timeout of concurrent calls.
            timeout = self.manager.options["upload_timeout"]
            http_api, ws_api = client._api._http_api, client._api._wsr_api
            if isinstance(http_api, HttpApi):
                http_api = HttpApi(http_api._api_root, http_api._access_token, timeout)
            if isinstance(ws_api, WebSocketReverseApi):
                ws_api = WebSocketReverseApi(ws_api._api_clients, ws_api._event_clients, timeout)
            api = UnifiedApi(http_api, ws_api)
        try:
            result = await api.call_action(action, **args)
        except NetworkError as exc:
            if "timeout" in str(exc).lower():
                raise TimeoutError from exc
            raise DeliveryError("QQ 协议连接中断，请先核对已发送的文件，再使用 resend") from exc
        except ApiNotAvailable as exc:
            raise DeliveryError("QQ 文件接口当前未连接，请检查 NapCat 连接后重试") from exc
        except ActionFailed as exc:
            code = exc.result.get("retcode")
            detail = f"（错误码 {code}）" if type(code) is int else ""
            raise DeliveryError(f"QQ 文件接口返回失败{detail}，请先核对已发送的文件，再使用 resend") from exc
        if not group or Manager._should_clean_sent(task):
            # Private uploads can explicitly return a null file ID. Treat that as unconfirmed
            # even when local cleanup is disabled.
            data = result.get("data", result) if isinstance(result, dict) else None
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("file_id"), str)
                or not data["file_id"].strip()
                or result.get("status", "ok") != "ok"
                or result.get("retcode", 0) != 0
            ):
                raise DeliveryError(
                    "上传未返回可确认成功的file_id，已保留本地文件；请核对已发送的文件并检查协议端版本，勿直接重复上传"
                )

    def _private_allowed(self, owner):
        owner = str(owner)
        allowed = self.config.get("allowed_private_users", [])
        return (
            self.config.get("private_enabled") is True
            and isinstance(allowed, list)
            and owner.isascii()
            and owner.isdigit()
            and (not allowed or any(owner == str(value).strip() for value in allowed))
        )

    def _allowed(self, route: dict):
        if not route["group"]:
            if not self._private_allowed(route["owner"]):
                raise ValueError("私聊未启用或此 QQ 号未获授权，请联系 AstrBot 管理员")
            return
        allowed = [str(x) for x in self.config.get("allowed_groups", [])]
        if allowed and route["group"] not in allowed:
            raise ValueError("本群不在插件允许列表中")

    def _line(self, task: dict):
        total = task["total"]
        progress = f"{task['done'] / total:.0%}" if total else "等待元数据"
        return (
            f"{task['id'][:8]} [{task['backend']}] {STATES.get(task['state'], task['state'])} "
            f"{progress} {size(task['done'])}/{size(total)} {size(task['speed'])}/s 用户:{task['owner']}"
        )

    def _default_backend(self) -> str:
        return next(iter(self.manager.backends), "aria2" if self.download_backend == "aria2_auto" else "")

    async def _submit_download(self, route, magnet, overrides=None):
        backend = self._default_backend()
        if self.download_backend == "aria2_auto" and backend not in self.manager.backends:
            raise DeliveryError("aria2 自动管理未启动。请检查 aria2 程序路径并重载插件。")
        return await self.manager.submit(route, magnet, backend, overrides)

    async def _send_preview_forward(self, event, text, screenshots, preview_id) -> str | None:
        """Return the plain-text fallback if the card send was not confirmed."""
        sender = str(event.message_obj.self_id)
        images = []
        try:
            async with asyncio.timeout(5):
                for index, url in enumerate(screenshots, 1):
                    data = await self.preview.image(url)
                    logger.info("磁力预览 %s：截图 %d %s", preview_id, index, "已获取" if data else "未获取")
                    if data:
                        images.append(Node(
                            name="磁力预览", uin=sender,
                            content=[Image.fromBytes(data)],
                        ))
        except TimeoutError:
            # Send the summary and any screenshots already collected.
            logger.info("磁力预览 %s：收集截图超时", preview_id)
        missing = len(screenshots) - len(images)
        if missing:
            text += f"\n有 {missing} 张预览图片未能获取，可查看截图链接。"
        nodes = [Node(name="磁力预览", uin=sender, content=[Plain(text)]), *images]
        try:
            await event.send(MessageChain([Nodes(nodes)]))
        except Exception as exc:
            result = exc.result if isinstance(exc, ActionFailed) else {}
            retcode = result.get("retcode")
            detail = str(result.get("message") or result.get("wording") or "")
            if isinstance(exc, ApiNotAvailable) or retcode == 1404:
                stage = "合并转发接口不可用"
            elif "生成节点为空" in detail or "文件处理失败" in detail:
                stage = "节点准备失败"
            elif "发送转发消息（res_id" in detail:
                stage = "已生成卡片，发送未确认"
            elif "timeout" in detail.lower() or "超时" in detail:
                stage = "协议端操作超时"
            else:
                stage = "协议端未提供可识别的失败阶段"
            logger.warning(
                "磁力预览 %s：聊天记录发送失败或未确认（%s，%s，OneBot 返回码 %s），改发纯文字",
                preview_id, stage, type(exc).__name__, retcode if type(retcode) is int else "未知",
            )
            if images:
                text += "\n带图片的预览发送失败或未确认，本次改发文字和截图链接。"
            return text
        logger.info("磁力预览 %s：聊天记录发送已确认，附图 %d 张", preview_id, len(images))
        return None

    async def _preview_results(self, event, target, chat):
        # AstrBot treats this flag as an opt-out of its default model reply.
        event.should_call_llm(True)
        preview_id = uuid4().hex[:8]
        try:
            result = await self.preview.lookup(target, chat)
        except ValueError as exc:
            logger.info("磁力预览 %s：查询未成功", preview_id)
            raise ValueError(f"{exc}（预览编号 {preview_id}）") from None
        async for reply in self._render_preview(event, result, preview_id):
            yield reply

    async def _render_preview(self, event, result, preview_id, position=""):
        logger.info("磁力预览 %s：查询成功，返回 %d 个截图链接", preview_id, len(result["screenshots"]))
        text = f"{position}{render(result)}\n预览编号：{preview_id}"
        preview_config = self.config.get("preview", {})
        screenshots = result["screenshots"] if preview_config.get("preview_inline_images", True) else ()
        if preview_config.get("preview_merge_forward", False):
            fallback = await self._send_preview_forward(event, text, screenshots, preview_id)
            if fallback is not None:
                try:
                    await event.send(MessageChain([Plain(fallback)]))
                except Exception as exc:
                    logger.warning("磁力预览 %s：纯文字保底发送未确认（%s）", preview_id, type(exc).__name__)
                else:
                    logger.info("磁力预览 %s：纯文字保底发送已确认", preview_id)
            return
        logger.info("磁力预览 %s：文字结果交由 AstrBot 发送", preview_id)
        yield event.plain_result(text)
        missing_images = 0
        failed_images = 0
        for index, url in enumerate(screenshots, 1):
            data = await self.preview.image(url)
            if not data:
                missing_images += 1
                logger.info("磁力预览 %s：截图 %d 未获取", preview_id, index)
                continue
            logger.info("磁力预览 %s：截图 %d 已获取", preview_id, index)
            try:
                await event.send(MessageChain([Image.fromBytes(data)]))
            except Exception as exc:
                # Avoid retrying an image with uncertain delivery. Try the next one.
                failed_images += 1
                retcode = exc.result.get("retcode") if isinstance(exc, ActionFailed) else None
                logger.warning(
                    "磁力预览 %s：第 %d 张截图发送失败（%s，OneBot 返回码 %s）",
                    preview_id, index, type(exc).__name__, retcode if type(retcode) is int else "未知",
                )
            else:
                logger.info("磁力预览 %s：截图 %d 发送已确认", preview_id, index)
        if missing_images or failed_images:
            issues = []
            if missing_images:
                issues.append(f"{missing_images} 张预览图片未能获取")
            if failed_images:
                issues.append(f"{failed_images} 张预览图片发送失败或未确认")
            yield event.plain_result(
                f"「{result['name']}」有 {'，'.join(issues)}，"
                f"可查看上方的截图链接（预览编号 {preview_id}）。"
            ).use_t2i(False).use_markdown(False)

    @filter.command("magnet-help", alias={"help"}, priority=-1)
    async def magnet_help(self, event: AstrMessageEvent):
        """查看磁力下载插件的全部命令。"""
        if (
            event.get_platform_name() == "aiocqhttp"
            and event.is_private_chat()
            and not self._private_allowed(event.get_sender_id())
        ):
            return
        return event.plain_result(HELP).use_t2i(False)

    @filter.command("magnet", alias={"磁力"})
    async def magnet(self, event: AstrMessageEvent):
        """管理当前聊天中的磁力下载任务；使用 /magnet 查看帮助。"""
        if not self.manager:
            yield event.plain_result("插件尚未初始化，请查看 AstrBot 日志")
            return
        if event.get_platform_name() != "aiocqhttp" or not (
            event.get_group_id() or event.is_private_chat()
        ):
            yield event.plain_result("仅支持 QQ OneBot 群聊和已授权的私聊")
            return
        # Read the original tail so links are not parsed as shell commands.
        text = event.message_str.strip()
        match = re.search(r"(?:^|\s)/?(?:magnet|磁力)(?:\s|$)", text, re.IGNORECASE)
        parts = text[match.end() :].strip().split() if match else []
        action = parts[0].lower() if parts else "help"
        route = self.route(event)
        store = self.manager.store
        settings = store.settings(route["chat"])
        try:
            if not route["group"]:
                event.should_call_llm(True)
                self._allowed(route)
                if action in ("on", "off", "auto"):
                    raise ValueError("私聊访问由 AstrBot 插件配置管理，这条命令仅用于群聊")
            if action == "help":
                response = HELP
            elif action == "backends":
                response = "当前下载器：" + (self._default_backend() or "未选择，请先在插件配置中选择")
                if self.download_backend == "aria2_auto" and self.managed_aria2:
                    response += "\n" + await self.managed_aria2.health()
            elif action == "test":
                if not await self._is_manager(event):
                    raise ValueError("连接测试需要机器人管理员或本群管理员权限")
                if len(parts) > 2:
                    raise ValueError("用法：/magnet test [后端名称]")
                name = parts[1] if len(parts) == 2 else self._default_backend()
                if name == "aria2" and self.download_backend == "aria2_auto" and self.managed_aria2:
                    response = await self.managed_aria2.health()
                elif name not in self.manager.backends:
                    raise ValueError("此下载器未启用，请先配置或查看 backends")
                elif self.manager.profiles[name]["kind"] == "xunlei_cli":
                    response = await self.manager.backends[name].health()
                else:
                    backend = self.manager.backends[name]
                    try:
                        if self.manager.profiles[name]["kind"] == "qb":
                            version = (await backend.call("app/version", get=True)).text.strip()
                        else:
                            version = (await backend.rpc("getVersion"))["version"]
                        response = f"{name} 连接正常，版本 {version}（未创建下载任务）"
                    except DeliveryError as exc:
                        response = str(exc)
                    except Exception:
                        response = f"{name} 连接失败，请检查服务是否启动、端口和凭据是否一致"
            elif action == "delivery":
                value = preferences(self.manager.delivery, settings)
                response = "当前会话新任务：" + describe(value)
                try:
                    if value["compress"]:
                        kind, _ = engine(self.manager.delivery, value["encrypt"])
                        response += "\n压缩引擎：" + ARCHIVER_NAMES[kind]
                except ValueError as exc:
                    response += "\n" + str(exc)
                response += "\n密码由机器人管理员在管理面板配置，聊天中不会显示。"
            elif action in ("compress", "encrypt", "autosend", "autodelete"):
                if not await self._is_manager(event):
                    raise ValueError("需要机器人管理员或本群管理员权限")
                self._allowed(route)
                if len(parts) != 2 or parts[1] not in ("on", "off", "default"):
                    raise ValueError(f"用法：/magnet {action} on|off|default")
                overrides = dict(settings.get("delivery", {}))
                key = {"autosend": "auto_send", "autodelete": "delete_after_send"}.get(
                    action, action
                )
                if (
                    action == "autodelete"
                    and parts[1] == "on"
                    and not preferences(self.manager.delivery, settings)["auto_send"]
                ):
                    raise ValueError("请先执行 /magnet autosend on，再开启自动发送后清理")
                if parts[1] == "default":
                    overrides.pop(key, None)
                else:
                    overrides[key] = parts[1] == "on"
                updated = dict(settings, delivery=overrides)
                value = snapshot(self.manager.delivery, updated)
                store.set_setting(route["chat"], "delivery", overrides)
                response = "当前会话新任务：" + describe(value) + "；已有任务不变。"
            elif action == "backend":
                response = "请在 AstrBot 的插件配置中选择下载器，保存后重载。"
            elif action in ("on", "off", "auto"):
                if not await self._is_manager(event):
                    raise ValueError("需要机器人管理员或本群管理员权限")
                self._allowed(route)
                if action in ("on", "off"):
                    store.set_setting(route["chat"], "enabled", action == "on")
                    response = "本群新任务已" + (
                        "启用" if action == "on" else "关闭；已有任务继续，可单独 cancel"
                    )
                else:
                    if len(parts) != 2 or parts[1] not in ("on", "off"):
                        raise ValueError("用法：/magnet auto on|off")
                    store.set_setting(route["chat"], "auto", parts[1] == "on")
                    response = "本群自动下载已" + ("开启" if parts[1] == "on" else "关闭")
            elif action in ("preview", "预览"):
                self._allowed(route)
                preview_config = self.config.get("preview", {})
                if not preview_config.get("preview_enabled", True) or not self.preview:
                    raise ValueError("预览功能未启用")
                if len(parts) != 2:
                    raise ValueError("用法：/magnet preview <磁力链接或当前会话的任务编号>")
                target = parts[1]
                if not target.lower().startswith("magnet:"):
                    target = store.resolve(route["chat"], target).get("magnet")
                    if not target:
                        raise ValueError("此任务资料已清理，无法再按任务编号预览")
                async for result in self._preview_results(event, target, route["chat"]):
                    yield result
                return
            elif action == "add":
                self._allowed(route)
                if route["group"] and not settings.get("enabled", False):
                    raise ValueError("本群未启用，请管理员执行 /magnet on")
                if len(parts) < 2:
                    raise ValueError("用法：/magnet add <磁力链接> [选项]")
                backend, overrides, named_backend = self._default_backend(), {}, False
                flags = {
                    "compress": "compress",
                    "encrypt": "encrypt",
                    "send": "auto_send",
                    "delete-after-send": "delete_after_send",
                }
                for token in parts[2:]:
                    if token.startswith("--"):
                        name = token[2:]
                        enabled = not name.startswith("no-")
                        name = name if enabled else name[3:]
                        if name not in flags or flags[name] in overrides:
                            raise ValueError("未知或重复的交付选项；使用 /magnet 查看帮助")
                        overrides[flags[name]] = enabled
                    elif not named_backend and token == backend:
                        named_backend = True  # Older commands may still name the selected downloader.
                    else:
                        raise ValueError("新任务使用插件选定的下载器。使用 /magnet backends 查看。")
                task = await self._submit_download(route, parts[1], overrides)
                response = (
                    f"已排队：{task['id'][:8]}，后端 {backend}。"
                    + describe(task["delivery"])
                    + "。使用 /magnet tasks 查看。"
                )
            elif action == "tasks":
                show_all = len(parts) > 1 and parts[1] == "all"
                tasks = store.list(route["chat"], active=not show_all)
                if not show_all:
                    # Unknown upload / failed stop needs action, so keep visible.
                    tasks += [
                        t
                        for t in store.list(route["chat"])
                        if t["state"]
                        in (
                            "ready",
                            "upload_unknown",
                            "upload_failed",
                            "cancel_failed",
                            "cleanup_failed",
                            "sent_cleanup_failed",
                        )
                    ]
                response = "当前聊天任务：\n" + (
                    "\n".join(self._line(t) for t in tasks[-20:]) or "无"
                )
            elif action in ("status", "cancel", "send", "resend", "cleanup"):
                if len(parts) != 2:
                    raise ValueError(f"用法：/magnet {action} <任务编号>")
                task = store.resolve(route["chat"], parts[1])
                if action == "status":
                    response = (
                        self._line(task)
                        + "\n"
                        + describe(task.get("delivery", DEFAULTS))
                        + f"；已确认发送 {task.get('sent_count', 0)} 个文件\n"
                        + (task["error"] or "")
                    )
                else:
                    if task["owner"] != route["owner"] and not await self._is_manager(event):
                        raise ValueError("只能操作自己的任务；群管理员可操作本群任务")
                    if action == "cancel":
                        await self.manager.cancel(task)
                        response = (
                            "已请求取消并清理该任务的下载文件和压缩包，使用 status 确认最终结果。"
                        )
                    elif action == "cleanup":
                        await self.manager.retry_sent_cleanup(task)
                        response = "已排队重试清理本地文件，不会重新上传。"
                    else:
                        await self.manager.resend(task, ready_only=action == "send")
                        response = (
                            "已排队发送到当前会话。"
                            if action == "send"
                            else "已排队重发未确认的文件；若此前已上传，会话中可能出现重复文件。"
                        )
            else:
                response = HELP
        except ValueError as exc:
            response = str(exc)
        except Exception:
            logger.warning("磁力插件指令操作失败（未记录链接、路径或凭据）")
            response = "操作失败，请检查插件配置和服务连接"
        yield event.plain_result(response)

    async def _scan_forward(self, event, route, links, limit):
        if self._forward_lock.locked():
            return
        card = next((part for part in event.get_messages() if isinstance(part, Forward)), None)
        if card is None or not card.id:
            return
        async with self._forward_lock:
            try:
                async with asyncio.timeout(5):
                    payload = await self._client(route["platform"]).call_action(
                        "get_forward_msg", message_id=card.id, self_id=route["self_id"]
                    )
            except (OneBotError, OSError, ValueError):
                return  # The normal message can still be processed if the card is unavailable.

        collect_magnets(forwarded_texts(payload), links, limit)

    def _auto_download_enabled(self, chat):
        settings = self.manager.store.settings(chat)
        return settings.get("enabled", False) and settings.get(
            "auto", self.config.get("auto_download", False)
        )

    def _forward_download_limit(self):
        options = self.manager.options
        limit = min(options["max_per_chat"], options["max_queue"])
        if self.config.get("forward_auto_limit_enabled", True):
            configured = self.config.get("forward_auto_max_links", 3)
            if type(configured) is not int:
                configured = 3
            limit = min(limit, max(1, configured))
        return limit

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def auto_magnet(self, event: AstrMessageEvent):
        """Handle links in ordinary QQ group messages according to the two opt-ins."""
        if not self.manager or event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            return
        text = event.message_str.strip()
        # The command handler owns explicit commands, including after wake-prefix removal.
        if (
            re.match(r"/?(?:magnet|磁力)(?:\s|$)", text, re.IGNORECASE)
            and (text.startswith("/") or event.is_at_or_wake_command)
        ):
            return
        route = self.route(event)
        if route["owner"] == route["self_id"]:
            return
        auto_download = self._auto_download_enabled(route["chat"])
        preview_config = self.config.get("preview", {})
        auto_preview = (
            preview_config.get("auto_preview", False)
            and preview_config.get("preview_enabled", True)
            and self.preview is not None
        )
        if not (auto_download or auto_preview):
            return
        try:
            self._allowed(route)
        except ValueError:
            return
        download_limit = 3
        links = {}
        collect_magnets((text,), links, 3)
        if self.config.get("scan_forwarded_magnets", False):
            if auto_download:
                download_limit = max(len(links), self._forward_download_limit())
            forward_limit = max(download_limit if auto_download else 0, 3 if auto_preview else 0)
            if len(links) < forward_limit:
                await self._scan_forward(event, route, links, forward_limit)
        if auto_download:
            for link in islice(links.values(), download_limit):
                # Reading the card and sending queue notices yield to other chat commands.
                if not self.manager or not self._auto_download_enabled(route["chat"]):
                    break
                try:
                    task = await self._submit_download(route, link)
                    yield event.plain_result(f"磁力任务已排队：{task['id'][:8]} [{task['backend']}]")
                except ValueError as exc:
                    yield event.plain_result("磁力任务：" + str(exc))
        if auto_preview and links:
            try:
                targets = list(islice(links.values(), 3))
                if len(targets) == 1:
                    async for reply in self._preview_results(event, targets[0], route["chat"]):
                        yield reply
                else:
                    event.should_call_llm(True)
                    outcomes = await self.preview.lookup_batch(targets, route["chat"])
                    for index, (key, result) in enumerate(outcomes, 1):
                        preview_id = uuid4().hex[:8]
                        position = f"第 {index}/{len(outcomes)} 条"
                        if isinstance(result, ValueError):
                            logger.info("磁力预览 %s：%s查询未成功", preview_id, position)
                            yield event.plain_result(
                                f"{position}磁力（{key[:8]}）：{result}。预览编号：{preview_id}"
                            ).use_t2i(False).use_markdown(False)
                            continue
                        async for reply in self._render_preview(
                            event, result, preview_id, position + " · "
                        ):
                            yield reply
            except ValueError:
                # Ordinary messages should not generate cooldown or service-error replies.
                pass

    async def terminate(self):
        if self.preview:
            await self.preview.close()
            self.preview = None
        if self.manager:
            await self.manager.close()
            self.manager = None
        if getattr(self, "managed_aria2", None):
            await self.managed_aria2.close()
            self.managed_aria2 = None
