"""Authenticated configuration UI helpers: list directories, never read file contents."""

import os
import stat
import string
from pathlib import Path

from .configuration import shared_download_directory
from .xunlei_cli import discover_cli

FIELDS = {
    "download_paths.directory",
    "aria2_auto.download_dir",
    "qb.download_dir",
    "aria2.download_dir",
    "xunlei_cli.download_dir",
}
PROGRAMS = {
    "aria2_auto.executable": ("aria2c.exe", "aria2c"),
    "xunlei_cli.executable": ("xunlei-cli.exe",),
}
FIELDS.update(PROGRAMS)
PAGE_SIZE = 300


def local_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("请选择有效的本机路径")
    value = value.strip()
    if os.name == "nt" and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if "\x00" in value or value.startswith(("\\\\", "//")):
        raise ValueError("目录浏览仅支持本机路径，不支持网络共享路径")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("请输入绝对路径")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("目录浏览不进入符号链接或目录联接")
    return path.resolve()


class PathPicker:
    def __init__(self, config: dict, data_root: Path):
        self.config, self.data_root = config, data_root

    def settings(self):
        paths = {}
        for field in sorted(FIELDS):
            group, key = field.split(".")
            paths[field] = str(self.config.get(group, {}).get(key, ""))
        return {
            "paths": paths,
            "detected_xunlei_cli": str(discover_cli(self.data_root.parent.parent) or ""),
            "default_directory": str((self.data_root / "downloads").resolve()),
            "shared_directory": str(shared_download_directory(self.config, self.data_root)),
            "message": "默认共用一个下载目录；仅在需要分别存放时设置独立目录。保存后重载插件生效。",
        }

    def browse(self, value: str = "", executable=False, page=1):
        program_names = PROGRAMS.get(executable, ())
        if executable is True:  # Preserve the aria2 picker API used by older views.
            program_names = PROGRAMS["aria2_auto.executable"]
        if value:
            path = local_path(value)
            if path.is_file():
                path = path.parent
        else:
            path = self.data_root.resolve()
            while not path.exists() and path != path.parent:
                path = path.parent
        if not path.is_dir():
            raise ValueError("目录不存在或不可访问")
        folders, files = [], []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    try:
                        linked = entry.is_symlink() or (
                            hasattr(candidate, "is_junction") and candidate.is_junction()
                        )
                        item = {"name": entry.name, "path": str(candidate)}
                        if linked:
                            # On Windows the link itself records whether it represents a directory.
                            if os.name == "nt" and (
                                entry.stat(follow_symlinks=False).st_file_attributes
                                & stat.FILE_ATTRIBUTE_DIRECTORY
                            ):
                                folders.append(dict(item, linked=True))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(item)
                        elif (
                            program_names
                            and entry.is_file(follow_symlinks=False)
                            and entry.name.lower() in program_names
                        ):
                            files.append(item)
                    except OSError:
                        continue
        except OSError:
            raise ValueError("无法浏览此目录，请选择有权限的文件夹") from None
        drives = (
            [f"{letter}:/" for letter in string.ascii_uppercase if Path(f"{letter}:/").is_dir()]
            if os.name == "nt"
            else ["/"]
        )
        folders.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        pages = max(1, (len(folders) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        offset = (page - 1) * PAGE_SIZE
        return {
            "path": str(path),
            "parent": str(path.parent),
            "drives": drives,
            "directories": folders[offset : offset + PAGE_SIZE],
            "files": sorted(files, key=lambda item: item["name"].casefold()),
            "page": page,
            "pages": pages,
            "total": len(folders),
        }

    def save(self, values: dict):
        if not isinstance(values, dict) or not values or any(key not in FIELDS for key in values):
            raise ValueError("只能保存本页的目录和下载器程序路径")
        validated = {}
        for field, value in values.items():
            if not isinstance(value, str):
                raise ValueError("路径格式错误")
            value = value.strip()
            if value:
                path = local_path(value)
                if field in PROGRAMS:
                    if not path.is_file() or path.name.lower() not in PROGRAMS[field]:
                        raise ValueError(f"请选择 {PROGRAMS[field][0]} 程序文件")
                elif not path.is_dir():
                    raise ValueError("请选择已存在的下载目录")
                value = str(path)
            validated[field] = value
        # Validate all fields before mutation; preserve credentials and enable flags.
        previous = {}
        try:
            for field, value in validated.items():
                group, key = field.split(".")
                previous[group] = dict(self.config.get(group, {}))
            for field, value in validated.items():
                group, key = field.split(".")
                self.config[group] = dict(self.config.get(group, {}), **{key: value})
            self.config.save_config()
        except Exception:
            for group, value in previous.items():
                self.config[group] = value
            raise ValueError("保存失败，请检查配置文件写入权限") from None
        return {"saved": True, "message": "路径已保存。请重载插件后使用新目录。"}
