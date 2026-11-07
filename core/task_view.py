"""Allowlisted administrator dashboard data; no magnets, paths, tokens, or passwords."""

import time

from .delivery import DEFAULTS, describe
from .manager import CANCELLABLE
from .models import ACTIVE

LABELS = {
    "queued": "排队",
    "starting": "提交中",
    "downloading": "下载中",
    "preparing": "准备文件",
    "compressing": "压缩中",
    "uploading": "上传中",
    "sent_cleaning": "已发送，清理中",
    "sent_cleanup_failed": "已发送，清理失败",
    "completed": "已交付",
    "ready": "本机已就绪",
    "cancelling": "取消与清理中",
    "cancelled": "已取消并清理",
    "failed": "失败",
    "cancel_failed": "移除任务失败",
    "cleanup_failed": "文件清理失败",
    "upload_unknown": "上传结果未知",
    "upload_failed": "上传失败",
}
ATTENTION = {
    "cancel_failed",
    "cleanup_failed",
    "sent_cleanup_failed",
    "upload_unknown",
    "upload_failed",
}


def listing(manager) -> dict:
    tasks = manager.store.list()
    # All active/attention jobs remain visible; bound the completed history.
    live = [
        task
        for task in tasks
        if task["state"] in ACTIVE or task["state"] in ATTENTION or task["state"] == "ready"
    ]
    live_ids = {task["id"] for task in live}
    history = [task for task in tasks if task["id"] not in live_ids][-100:]
    groups = {}
    for task in live + history:
        chat = task["chat"]
        group = groups.setdefault(
            chat,
            dict(
                chat=chat,
                platform=task.get("platform", "未知实例"),
                group=task.get("group", "未知群聊"),
                tasks=[],
            ),
        )
        state = task["state"]
        total, done = int(task.get("total", 0)), int(task.get("done", 0))
        group["tasks"].append(
            dict(
                id=task["id"],
                short_id=task["id"][:8],
                name=task.get("name") or "等待资源信息",
                backend=task["backend"],
                owner=task["owner"],
                state=state,
                label="已交付并清理"
                if state == "completed" and task.get("cleaned")
                else LABELS.get(state, state),
                total=total,
                done=done,
                progress=min(100, max(0, done / total * 100)) if total else None,
                speed=int(task.get("speed", 0)),
                created=task["created"],
                error=task.get("error", ""),
                delivery=describe(task.get("delivery", DEFAULTS)),
                sent_count=task.get("sent_count", 0),
                cancellable=state in CANCELLABLE and state != "cancelling",
                retry_cleanup=state == "sent_cleanup_failed" and bool(task.get("delivered")),
                delivered=bool(task.get("delivered")),
                live=task["id"] in live_ids,
                attention=state in ATTENTION,
                cleaned=bool(task.get("cleaned")),
            )
        )
    return dict(
        groups=sorted(groups.values(), key=lambda item: (item["platform"], item["group"])),
        summary=dict(
            active=sum(task["state"] in ACTIVE for task in tasks),
            attention=sum(task["state"] in ATTENTION for task in tasks),
            speed=sum(
                int(task.get("speed", 0)) for task in tasks if task["state"] == "downloading"
            ),
        ),
        updated_at=time.time(),
        history_limit=100,
    )
