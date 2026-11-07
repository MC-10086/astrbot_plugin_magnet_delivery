"""Delivery preferences and discovery of supported local compression programs."""

import hashlib
import os
import shutil
from pathlib import Path

DEFAULTS = {"compress": True, "encrypt": False, "auto_send": True, "delete_after_send": True}
ARCHIVER_NAMES = {"windows": "Windows 系统 ZIP", "builtin": "内置 ZIP", "7zip": "外部压缩软件"}


class DeliveryError(ValueError):
    """Safe, user-facing errors without paths, external output, or passwords."""


class DownloadNotSubmitted(DeliveryError):
    """The current attempt stopped before issuing the download request."""


def configuration(value: dict | None = None) -> dict:
    value = value or {}
    result = {key: bool(value.get(key, default)) for key, default in DEFAULTS.items()}
    result.update(
        archiver=value.get("archiver", "auto"),
        executable=str(value.get("executable", "")).strip(),
        password=str(value.get("password", "")),
    )
    if result["archiver"] not in ("auto", "builtin", "7zip"):
        raise DeliveryError("压缩方式无效，请管理员在插件配置中重新选择")
    return result


def preferences(defaults: dict, settings: dict) -> dict:
    overrides = settings.get("delivery", {})
    result = {key: overrides.get(key, defaults.get(key, value)) for key, value in DEFAULTS.items()}
    if not result["auto_send"]:
        result["delete_after_send"] = False
    return result


def describe(value: dict) -> str:
    return "、".join(
        (
            "自动压缩" if value["compress"] else "不压缩",
            "密码保护" if value["encrypt"] else "不加密",
            "自动发送" if value["auto_send"] else "仅保存本机",
            "发送成功后清理本地文件"
            if value.get("auto_send") and value.get("delete_after_send")
            else "保留本地文件",
        )
    )


def find_7zip(explicit: str = "") -> str:
    if explicit:
        path = Path(explicit)
        if (
            path.is_absolute()
            and path.is_file()
            and path.name.lower() in ("7z.exe", "7zz.exe", "7z", "7zz", "7za.exe", "7za")
        ):
            return str(path.resolve())
        raise DeliveryError("压缩软件路径无效，请管理员在插件配置中检查程序路径")
    candidates = []
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                for name in ("7zFM.exe", "7z.exe"):
                    try:
                        with winreg.OpenKey(
                            hive,
                            r"Software\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + name,
                            0,
                            winreg.KEY_READ | view,
                        ) as key:
                            program = str(winreg.QueryValueEx(key, "")[0]).strip('"')
                            candidates.append(Path(program).with_name("7z.exe"))
                    except OSError:
                        pass
                try:
                    with winreg.OpenKey(hive, r"Software\7-Zip", 0, winreg.KEY_READ | view) as key:
                        candidates.append(Path(winreg.QueryValueEx(key, "Path")[0]) / "7z.exe")
                except OSError:
                    pass
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / "7-Zip" / "7z.exe")
    for name in ("7zz", "7z", "7za"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return next((str(path.resolve()) for path in candidates if path.is_file()), "")


def engine(config: dict, encrypted: bool) -> tuple[str, str]:
    mode = config.get("archiver", "auto")
    if mode == "builtin":
        if encrypted:
            raise DeliveryError("内置 ZIP 不支持密码保护，请管理员选择支持加密的压缩方式")
        return "builtin", ""
    if mode == "7zip" or encrypted:
        program = find_7zip(config.get("executable", ""))
        if not program:
            raise DeliveryError("未找到可用的压缩软件，请管理员检查插件的压缩设置")
        return "7zip", program
    if os.name == "nt" and os.environ.get("SystemRoot"):
        program = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if program.is_file():
            return "windows", str(program)
    return "builtin", ""


def password_tag(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def snapshot(config: dict, settings: dict, overrides: dict | None = None) -> dict:
    result = preferences(config, settings)
    if overrides:
        if any(
            key not in DEFAULTS or not isinstance(value, bool) for key, value in overrides.items()
        ):
            raise DeliveryError("单次交付选项无效")
        result.update(overrides)
    if not result["auto_send"]:
        if overrides and overrides.get("delete_after_send"):
            raise DeliveryError("发送后删除本地文件需要先开启自动发送到原群")
        result["delete_after_send"] = False
    if result["encrypt"] and not result["compress"]:
        raise DeliveryError("密码保护需要自动压缩；请开启 compress 或关闭 encrypt")
    if result["compress"]:
        engine(config, result["encrypt"])
    if result["encrypt"]:
        password = config.get("password", "")
        if (
            not password
            or len(password) > 99
            or '"' in password
            or any(not 32 <= ord(c) <= 126 for c in password)
        ):
            raise DeliveryError(
                "请在管理面板填写1至99位英文、数字或符号作为ZIP密码（不含中文、双引号）"
            )
        # A pending task must not silently use a different password after a configuration edit.
        result["password_tag"] = password_tag(password)
    return result


def task_password(task: dict, config: dict) -> str:
    delivery = task.get("delivery", DEFAULTS)
    if not delivery.get("encrypt"):
        return ""
    password = config.get("password", "")
    if not password or password_tag(password) != delivery.get("password_tag"):
        raise DeliveryError("压缩密码与提交任务时不一致，已停止压缩；请检查密码配置")
    return password
