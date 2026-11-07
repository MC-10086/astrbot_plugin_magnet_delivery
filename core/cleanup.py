"""Delete only validated per-task paths; never follow links into other directories."""

import os
import re
import shutil
from pathlib import Path


def checked_directory(path: Path) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("清理路径包含链接，请管理员检查目录")
    return path.resolve()


def task_directory(download_root: Path, task_id: str, *, archive: bool = False) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("任务编号无效")
    parent = checked_directory(download_root)
    name = task_id + (".archives" if archive else "")
    target = checked_directory(parent / name)
    if target.parent != parent or target.name != name:
        raise ValueError("任务目录越界")
    return target


def cleanup_task(data_root: Path, download_root: Path | None, task_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("任务编号无效，拒绝清理")
    data_root = checked_directory(data_root)
    directories = []
    if download_root is not None:
        directories = [
            task_directory(download_root, task_id),
            task_directory(download_root, task_id, archive=True),
        ]
        # Validate both final absolute targets before recursive deletion.
        for target in directories:
            if target.exists() and not target.is_dir():
                raise ValueError("任务下载目录类型异常")
        for target in directories:
            if target.exists():
                # Python 3.12 rmtree does not traverse symlinks or junctions.
                shutil.rmtree(target)
    archives = checked_directory(data_root / "archives")
    if archives.parent != data_root:
        raise ValueError("压缩目录越界，拒绝清理")
    if archives.is_dir():
        for item in archives.iterdir():
            if not item.name.startswith(task_id + "."):
                continue
            if item.is_symlink():
                item.unlink()
            elif hasattr(item, "is_junction") and item.is_junction():
                os.rmdir(item)
            elif item.is_file():
                item.unlink()
            else:
                raise ValueError("任务临时文件类型异常")
        if any(item.name.startswith(task_id + ".") for item in archives.iterdir()):
            raise OSError("Task archive artifacts remain")
    if any(os.path.lexists(directory) for directory in directories):
        raise OSError("Task directory remains")
