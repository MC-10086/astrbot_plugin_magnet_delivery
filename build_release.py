"""Build a local AstrBot ZIP without caches, credentials, or runtime data."""

import hashlib
import json
import zipfile
from pathlib import Path

SOURCE_FILES = (
    ".gitignore",
    "__init__.py",
    "_conf_schema.json",
    "build_release.py",
    "CHANGELOG.md",
    "logo.png",
    "main.py",
    "metadata.yaml",
    "pyproject.toml",
    "README.md",
    "requirements.txt",
    ".astrbot-plugin/i18n/zh-CN.json",
    "views/paths/app.js",
    "views/paths/index.html",
    "views/paths/style.css",
    "views/tasks/app.js",
    "views/tasks/index.html",
    "views/tasks/style.css",
)


def source_files(root: Path) -> list[Path]:
    """Select the public source tree without walking local data or scratch files."""
    files = [root / name for name in SOURCE_FILES]
    files.extend((root / "core").glob("*.py"))
    files.extend((root / "tests").glob("test_*.py"))
    for path in files:
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Missing or linked source file: {path.relative_to(root)}")
    return sorted(files)


def main():
    root = Path(__file__).resolve().parent
    json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    version = next(line.split()[1] for line in (root / "metadata.yaml").read_text(
        encoding="utf-8"
    ).splitlines() if line.startswith("version: "))
    destination = root.parent / f"astrbot_plugin_magnet_delivery-{version}.zip"
    files = source_files(root)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as package:
        for path in files:
            package.write(path, path.relative_to(root.parent).as_posix())
    with zipfile.ZipFile(destination) as package:
        assert package.testzip() is None
        assert root.name + "/metadata.yaml" in package.namelist()
        assert root.name + "/main.py" in package.namelist()
        assert root.name + "/logo.png" in package.namelist()
    print(destination.name)
    print("SHA256", hashlib.sha256(destination.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
