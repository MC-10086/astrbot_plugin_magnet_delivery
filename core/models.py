"""Protocol-neutral models; no dependency on AstrBot."""

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ACTIVE = (
    "queued",
    "starting",
    "downloading",
    "preparing",
    "compressing",
    "uploading",
    "sent_cleaning",
    "cancelling",
)


def normalize_magnet(value: str) -> tuple[str, str]:
    """Return a canonical BT v1 magnet and its hexadecimal infohash."""
    if len(value) > 16384:
        raise ValueError("磁力链接过长")
    if value[:8].lower() == "magnet:？":
        value = "magnet:?" + value[8:]
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "magnet":
        raise ValueError("请输入 magnet: 链接")
    query = parse_qs(parsed.query)
    hashes = []
    for xt in query.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            raw = xt[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", raw):
                hashes.append(raw.lower())
            elif re.fullmatch(r"[A-Za-z2-7]{32}", raw):
                hashes.append(base64.b32decode(raw.upper()).hex())
    if len(set(hashes)) != 1:
        raise ValueError("首版仅支持单一 BTIH 磁力（40位十六进制或32位Base32）")
    infohash = hashes[0]
    # Do not forward xs/as/ws URL parameters to the downloader.
    pairs = [("xt", "urn:btih:" + infohash)]
    for tracker in query.get("tr", [])[:20]:
        p = urlsplit(tracker)
        if p.scheme in ("http", "https", "udp") and p.hostname:
            pairs.append(("tr", tracker))
    return "magnet:?" + urlencode(pairs), infohash


def chat_key(platform: str, group: str, sender: str) -> str:
    """Group tasks are shared within that group, regardless of per-user UMO."""
    import json

    return json.dumps([platform, "group" if group else "private", group or sender])


def inside(root: Path, path: Path) -> Path:
    root, resolved = root.resolve(), path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("文件路径超出任务目录")
    # Check all components, including junctions on Windows.
    cursor = path.absolute()
    while cursor != root and cursor != cursor.parent:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError("任务包含符号链接或目录联接")
        cursor = cursor.parent
    return resolved


@dataclass
class Snapshot:
    """A backend's progress report. A replacement ID is persisted before polling again."""

    total: int = 0
    done: int = 0
    speed: int = 0
    complete: bool = False
    files: list[tuple[str, int]] = field(default_factory=list)
    remote_id: str = ""
    name: str = ""
    total_is_estimate: bool = False


class Backend:
    """Backends must confirm removal on cancel before local files can be cleaned."""

    async def start(self, task: dict) -> str:
        raise NotImplementedError

    async def poll(self, task: dict) -> Snapshot:
        raise NotImplementedError

    async def stop(self, task: dict) -> None:
        raise NotImplementedError

    async def abort(self, task: dict) -> None:
        """Stop a failed task while retaining its local files."""
        await self.stop(task)

    async def cancel(self, task: dict) -> None:
        await self.stop(task)

    async def close(self) -> None:
        pass
