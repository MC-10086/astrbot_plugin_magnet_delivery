"""Translate simple form fields and advanced JSON into the same backend profiles."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .xunlei_cli import discover_cli

DOWNLOAD_BACKENDS = ("none", "xunlei_cli", "qb", "aria2_auto", "aria2", "advanced")

PREVIEW_KEYS = (
    "preview_enabled",
    "auto_preview",
    "preview_inline_images",
    "preview_merge_forward",
    "preview_proxy",
    "preview_image_hosts",
    "preview_cooldown",
    "preview_cache_seconds",
)


def migrate_preview_config(config: dict) -> bool:
    """Move old top-level settings into the preview form once."""
    preview = config.get("preview", {})
    if preview.get("legacy_migrated"):
        return False
    # AstrBot drops keys missing from the schema before the plugin starts.
    # Keep the old fields hidden so the loader can pass their values through.
    legacy = {key: config[key] for key in PREVIEW_KEYS if key in config}
    config["preview"] = {**preview, **legacy, "legacy_migrated": True}
    if "preview_proxy" in config:
        config["preview_proxy"] = "system"  # Keep proxy credentials only in the new field.
    return True


def load_profiles(raw: str, default_root: Path | None = None) -> dict:
    """Keep the original JSON format and profile identities backward compatible."""
    try:
        entries = json.loads(raw.strip() or "[]")
    except (ValueError, AttributeError):
        # Never include the JSON text: it can contain passwords.
        raise ValueError("高级下载器配置不是有效 JSON，请检查格式或留空") from None
    if not isinstance(entries, list):
        raise ValueError("高级下载器配置必须是 JSON 数组")
    profiles = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("后端配置项必须是对象")
        if not entry.get("enabled", True):
            continue
        name = entry.get("name", "")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name)
            or name in profiles
        ):
            raise ValueError("后端名称需为唯一的小写字母数字名称")
        if entry.get("kind") in ("xunlei", "xunlei_cli"):
            raise ValueError("迅雷请通过内置下载器配置启用")
        if entry.get("kind") not in ("qb", "aria2"):
            raise ValueError("高级 JSON 下载器类型须为 qb 或 aria2")
        try:
            address = entry.get("url")
            if not isinstance(address, str):
                raise ValueError
            url = urlsplit(address)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.port == 0
            ):
                raise ValueError
        except ValueError:
            # Invalid URLs can contain pasted credentials. Keep the raw value out of errors.
            raise ValueError("后端 URL 必须是无嵌入凭据的有效 HTTP(S) 地址") from None
        directory = str(entry.get("local_root", "")).strip()
        root = Path(directory) if directory else default_root or Path("")
        if not root.is_absolute():
            raise ValueError("下载目录必须是本机绝对路径")
        profiles[name] = dict(entry, local_root=str(root.resolve()))
    return profiles


def _port(value, label: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(label + "端口必须为1到65535的整数")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(label + "端口必须为1到65535的整数")
    return port


def shared_download_directory(config: dict, data_root: Path) -> Path:
    settings = config.get("download_paths", {})
    if not isinstance(settings, dict):
        raise ValueError("统一下载目录配置格式异常")
    value = str(settings.get("directory", "")).strip()
    root = Path(value) if value else data_root / "downloads"
    if not root.is_absolute():
        raise ValueError("统一下载目录必须为绝对路径，或留空自动创建")
    return root.resolve()


def download_directory(config: dict, data_root: Path, name: str) -> Path:
    form = config.get(name, {})
    if not isinstance(form, dict):
        raise ValueError("下载器配置格式异常")
    value = str(form.get("download_dir", "")).strip()
    root = Path(value) if value else shared_download_directory(config, data_root)
    if not root.is_absolute():
        raise ValueError("独立下载目录必须为绝对路径，或留空跟随统一目录")
    return root.resolve()


def download_selection(config: dict) -> tuple[str, str]:
    selected = config.get("download_backend", "legacy")
    if selected == "xunlei":
        return "xunlei_cli", ""
    if selected in DOWNLOAD_BACKENDS and not (selected == "none" and config.get("default_backend")):
        return selected, config.get("advanced_name", "")
    if selected not in ("none", "legacy"):
        raise ValueError("请选择一个下载器")
    existing = {
        "aria2" if name == "aria2_auto" else name: (name, "")
        for name in ("qb", "aria2", "aria2_auto", "xunlei_cli")
        if config.get(name, {}).get("enabled")
    }
    try:
        custom = json.loads(config.get("backends_json", "[]").strip() or "[]")
    except (ValueError, AttributeError):
        custom = []
    if isinstance(custom, list):
        for item in custom:
            if isinstance(item, dict) and item.get("enabled", True) and isinstance(item.get("name"), str):
                existing.setdefault(item["name"], ("advanced", item["name"]))
    preferred = config.get("default_backend", "qb")
    if preferred in existing:
        return existing[preferred]
    if preferred == "xunlei":
        return "xunlei_cli", ""
    if len(existing) == 1:
        return next(iter(existing.values()))
    return "none", ""


def configured_profiles(
    config: dict, data_root: Path, *, selected: str | None = None, create_dirs: bool = True
) -> dict:
    """Simple built-ins are opt-in; advanced profiles never get silently overwritten."""
    if selected in ("aria2_auto", "none"):
        return {}
    profiles = {}
    if selected in (None, "advanced"):
        profiles = load_profiles(
            config.get("backends_json", "[]"), shared_download_directory(config, data_root)
        )
    if selected == "advanced":
        name = config.get("advanced_name", "").strip()
        if not name and len(profiles) == 1:
            name = next(iter(profiles))
        if name not in profiles:
            raise ValueError("高级下载器请填写一个有效的配置名称")
        profiles = {name: profiles[name]}
    for name, label, default_port in (("qb", "qBittorrent", 8080), ("aria2", "aria2", 6800)):
        if selected not in (None, name):
            continue
        form = config.get(name, {})
        if not isinstance(form, dict):
            raise ValueError(label + "配置格式异常，请在配置页面重新填写")
        if selected is None and not form.get("enabled", False):
            continue
        if name in profiles:
            raise ValueError(
                f"下载器名称 {name} 重复：请关闭对应内置配置，或修改高级 JSON 中的 name"
            )
        port = _port(form.get("port", default_port), label)
        root = download_directory(config, data_root, name)
        entry = {
            "name": name,
            "kind": name,
            "url": f"http://127.0.0.1:{port}",
            "local_root": str(root.resolve()),
        }
        if name == "qb":
            entry.update(username=form.get("username", "admin"), password=form.get("password", ""))
        else:
            entry.update(url=entry["url"] + "/jsonrpc", secret=form.get("secret", ""))
        if not str(form.get("download_dir", "")).strip():
            # Only the pre-0.8 built-in defaults are eligible for task recovery.
            entry["legacy_local_roots"] = [str((data_root / "downloads" / name).resolve())]
        profiles[name] = entry
    cli = config.get("xunlei_cli", {})
    if selected == "xunlei_cli" or (selected is None and cli.get("enabled")):
        if "xunlei_cli" in profiles:
            raise ValueError("高级 JSON 已使用 xunlei_cli 作为下载器名称，请换名")
        executable = str(cli.get("executable", "")).strip().strip('"')
        executable = executable or str(discover_cli(data_root.parent.parent) or "")
        profiles["xunlei_cli"] = dict(
            name="xunlei_cli", kind="xunlei_cli", executable=executable,
            local_root=str(download_directory(config, data_root, "xunlei_cli")),
            max_files=max(1, int(config.get("max_files", 2000))),
        )
    # Validate everything first, then create only directories for enabled backends.
    if create_dirs:
        for profile in profiles.values():
            Path(profile["local_root"]).mkdir(parents=True, exist_ok=True)
    return profiles
