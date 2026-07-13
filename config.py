from __future__ import annotations

import os
import sys
import shutil
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "music2bb"
LEGACY_APP_NAME = "kg2bb"
MIGRATION_MARKER = ".migration-v2"


DEFAULT_BLOCK_KEYWORDS: list[str] = [
    "翻唱", "cover", "Cover", "COVER", "伴奏", "instrumental",
    "教程", "教学", "tutorial", "乐谱", "sheet", "钢琴教学",
    "吉他教学", "尤克里里", "八音盒", "小提琴", "竖琴", "口琴",
    "纯音乐", "编曲", "作曲", "remix", "Remix", "Remake",
    "remake", "翻奏", "合奏", "演奏", "改编", "混音",
    "钢琴", "吉他", "电子琴", "竹笛", "古筝", "二胡",
    "大提琴", "中提琴", "萨克斯", "架子鼓", "手风琴", "木吉他",
    "Fingerstyle", "fingerstyle", "指弹",
]

DEFAULT_QUALITY_KEYWORDS: list[str] = [
    "官方", "official", "Official", "OFFICIAL", "MV", "mv",
    "无损", "flac", "FLAC", "Hi-Res", "hi-res", "HIRES",
    "4K", "杜比", "Dolby", "全景声", "录音室", "Studio",
    "Live", "live", "LIVE", "现场", "演唱会",
]

DEFAULT_WEIGHTED_UPLOADERS: list[str] = [
    "HOYO-MiX", "崩坏星穹铁道", "原神", "崩坏3",
]


@dataclass
class Paths:
    dir: str
    cache_dir: str
    cookie_dir: str = ""
    block_file: str = ""
    quality_file: str = ""
    uploader_file: str = ""
    migration_marker: str = ""

    def __post_init__(self):
        self.cookie_dir = os.path.join(self.dir, "cookies")
        self.block_file = os.path.join(self.dir, "b.txt")
        self.quality_file = os.path.join(self.dir, "w.txt")
        self.uploader_file = os.path.join(self.dir, "w-up.txt")
        self.migration_marker = os.path.join(self.dir, MIGRATION_MARKER)


@dataclass
class MigrationResult:
    already_complete: bool = False
    copied: list[str] = field(default_factory=list)


@dataclass
class Config:
    paths: Paths
    block_keywords: list[str] = field(default_factory=list)
    quality_keywords: list[str] = field(default_factory=list)
    weighted_uploaders: list[str] = field(default_factory=list)
    migration: MigrationResult = field(default_factory=MigrationResult)


def _default_base_dirs() -> tuple[str, str]:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return (
            os.path.join(home, "Library", "Application Support", APP_NAME),
            os.path.join(home, "Library", "Caches", APP_NAME),
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        localappdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return (
            os.path.join(appdata, APP_NAME),
            os.path.join(localappdata, APP_NAME),
        )
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        cache_home = os.environ.get("XDG_CACHE_HOME", os.path.join(home, ".cache"))
        return os.path.join(config_home, APP_NAME), os.path.join(cache_home, APP_NAME)


def resolve_paths(config_dir: str = "", cache_dir: str = "") -> Paths:
    if not config_dir or not cache_dir:
        default_dir, default_cache = _default_base_dirs()
        if not config_dir:
            config_dir = default_dir
        if not cache_dir:
            cache_dir = default_cache
    return Paths(
        dir=os.path.abspath(config_dir),
        cache_dir=os.path.abspath(cache_dir),
    )


def _parse_keywords(filepath: str) -> list[str]:
    p = Path(filepath)
    if not p.exists():
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        kw = line.strip()
        if kw and not kw.startswith("#"):
            if kw not in seen:
                seen.add(kw)
                keywords.append(kw)
    return keywords


def _atomic_write(filepath: str, data: str) -> None:
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".music2bb-")
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(p))
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _migrate_legacy(paths: Paths, legacy_dirs: list[str]) -> MigrationResult:
    marker = Path(paths.migration_marker)
    if marker.exists():
        return MigrationResult(already_complete=True)

    candidates = [
        ("bilibili.json", lambda d: os.path.join(d, ".cookies", "bilibili.json"), os.path.join(paths.cookie_dir, "bilibili.json")),
        ("b.txt", lambda d: os.path.join(d, "b.txt"), paths.block_file),
        ("w.txt", lambda d: os.path.join(d, "w.txt"), paths.quality_file),
        ("w-up.txt", lambda d: os.path.join(d, "w-up.txt"), paths.uploader_file),
    ]

    result = MigrationResult()
    for name, legacy_fn, target in candidates:
        if Path(target).exists():
            continue
        for legacy_dir in legacy_dirs:
            source = legacy_fn(legacy_dir)
            if Path(source).exists():
                try:
                    _atomic_write(target, Path(source).read_text(encoding="utf-8"))
                    result.copied.append(name)
                except OSError:
                    pass
                break

    try:
        _atomic_write(paths.migration_marker, "1\n")
    except OSError:
        pass

    return result


def _materialize_list(target: str, defaults: list[str]) -> None:
    p = Path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        content = "# 一行一个关键词，以 # 开头的行为注释\n" + "\n".join(defaults) + "\n"
        _atomic_write(target, content)


def load_config(
    config_dir: str = "",
    cache_dir: str = "",
    legacy_dir: str = "",
    skip_migration: bool = False,
) -> Config:
    paths = resolve_paths(config_dir, cache_dir)

    migration = MigrationResult()
    if not skip_migration:
        legacy_dirs: list[str] = []
        if legacy_dir:
            legacy_dirs.append(legacy_dir)
        else:
            try:
                legacy_dirs.append(os.path.abspath("."))
            except OSError:
                pass
        migration = _migrate_legacy(paths, legacy_dirs)

    _materialize_list(paths.block_file, DEFAULT_BLOCK_KEYWORDS)
    _materialize_list(paths.quality_file, DEFAULT_QUALITY_KEYWORDS)
    _materialize_list(paths.uploader_file, DEFAULT_WEIGHTED_UPLOADERS)

    blocks = _parse_keywords(paths.block_file)
    if not blocks:
        blocks = DEFAULT_BLOCK_KEYWORDS.copy()

    quality = _parse_keywords(paths.quality_file)
    if not quality:
        quality = DEFAULT_QUALITY_KEYWORDS.copy()

    uploaders = _parse_keywords(paths.uploader_file)
    if not uploaders:
        uploaders = DEFAULT_WEIGHTED_UPLOADERS.copy()

    return Config(
        paths=paths,
        block_keywords=blocks,
        quality_keywords=quality,
        weighted_uploaders=uploaders,
        migration=migration,
    )
