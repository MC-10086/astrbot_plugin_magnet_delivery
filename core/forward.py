"""Read outer OneBot forward records and collect magnets from message text."""

import re

from .models import normalize_magnet

_MAGNET_LINK = re.compile(
    r"magnet:[?？][^\s<>\"'，。；）]+?(?=magnet:[?？]|[\s<>\"'，。；）]|$)",
    re.IGNORECASE,
)


def collect_magnets(texts, links, limit):
    """Fill the shared per-message limit with distinct, valid BTIH links."""
    for text in texts:
        for match in _MAGNET_LINK.finditer(text):
            candidate = match.group()
            try:
                link, infohash = normalize_magnet(candidate)
            except ValueError:
                # Chat punctuation, such as a closing Markdown parenthesis, can touch the hash.
                trimmed = candidate.rstrip(").,!?;]}")
                if trimmed == candidate:
                    continue
                try:
                    link, infohash = normalize_magnet(trimmed)
                except ValueError:
                    continue
            links.setdefault(infohash, link)
            if len(links) >= limit:
                return


def forwarded_texts(payload):
    if not isinstance(payload, dict):
        return
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(nodes := data.get("messages"), list):
        return
    for node in nodes[:99]:
        if not isinstance(node, dict):
            continue
        # OneBot can return message records or wrap them in segments of type node.
        record = node.get("data") if node.get("type") == "node" else node
        if not isinstance(record, dict):
            continue
        segments = record.get("message") or record.get("content")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            fields = segment.get("data")
            if isinstance(fields, dict) and isinstance(text := fields.get("text"), str):
                yield text
