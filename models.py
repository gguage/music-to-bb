from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# 歌手名映射表：将中文/日文名映射为常用的搜索关键词
ARTIST_ALIASES = {
    "知更鸟": ["Robin", "知更鸟", "崩坏星穹铁道"],
    "HOYO": ["HOYO-MiX", "米哈游", "miHoYo"],
    "HOYO-MiX": ["HOYO-MiX", "米哈游", "miHoYo"],
    "初音ミク": ["初音未来", "Miku", "Hatsune Miku"],
    "ミク": ["初音未来", "Miku"],
}


class QualityLevel(Enum):
    LOW = 1
    STANDARD = 2
    HIGH = 3
    LOSSLESS = 4
    HI_RES = 5


@dataclass
class KugouSong:
    name: str
    artist: str
    album: str = ""
    duration: str = ""
    hash: str = ""

    def _clean_name(self) -> str:
        """清理歌名，保留核心信息"""
        name = self.name.strip()
        
        # 提取括号内的关键词（如From "Zootopia"、From "Zootopia 2"）
        from_match = re.search(r'\(From\s+"([^"]+)"\)', name)
        if from_match:
            # 保留From后面的关键词（去掉引号）
            from_keyword = from_match.group(1).strip()
            name = re.sub(r'\s*\(From\s+"[^"]+"\)\s*', '', name)
            # 如果from_keyword包含数字或版本，去掉（如"Zootopia 2" -> "Zootopia"）
            from_keyword = re.sub(r'\s*\d+$', '', from_keyword).strip()
            if from_keyword:
                name = f"{name} {from_keyword}"
        
        # 去掉其他括号内容（如feat信息、翻译等）
        name = re.sub(r'\s*\([^)]*\)\s*', ' ', name)
        name = re.sub(r'\s*（[^）]*）\s*', ' ', name)
        name = re.sub(r'\s*\[[^\]]*\]\s*', ' ', name)
        name = re.sub(r'\s*【[^】]*】\s*', ' ', name)
        
        # 去掉 feat. 及其后内容
        name = re.sub(r'\s*(feat\.|Feat\.|FEAT\.|feat|Feat|FEAT)\s*.*$', '', name, flags=re.IGNORECASE)
        
        # 去掉 - 及其后内容（如歌手名重复）
        name = re.sub(r'\s*-\s*.*$', '', name)
        
        # 清理多余空格
        name = re.sub(r'\s+', ' ', name).strip()
        
        return name

    def _clean_artist(self) -> str:
        """清理歌手名，保留关键信息"""
        artist = self.artist.strip()
        
        # 保留重要的关键词（如HOYO、米哈游等）
        keywords_to_keep = ['HOYO', 'Hoyo', 'hoyo', '米哈游', 'miHoYo', 'mihoyo']
        kept_keywords = []
        for kw in keywords_to_keep:
            if kw in artist:
                kept_keywords.append(kw)
        
        # 去掉括号内容
        artist = re.sub(r'\([^)]*\)', '', artist)
        artist = re.sub(r'（[^）]*）', '', artist)
        
        # 取第一个主要歌手
        for sep in [',', '、', '/', '&']:
            if sep in artist:
                artist = artist.split(sep)[0].strip()
                break
        
        # 加上保留的关键词
        for kw in kept_keywords:
            if kw not in artist:
                artist = f"{artist} {kw}"
        
        return artist.strip()

    @property
    def search_keyword(self) -> str:
        """搜索关键词（清理后的歌曲名）"""
        return self._clean_name()

    @property
    def search_keyword_full(self) -> str:
        """完整搜索关键词，使用别名映射提高匹配率"""
        name = self._clean_name()
        artist = self._clean_artist()
        
        # 检查是否有别名映射
        search_keywords = [name]
        if artist:
            # 检查歌手名是否在映射表中
            for key, aliases in ARTIST_ALIASES.items():
                if key in artist or key in self.artist:
                    # 使用映射的别名作为额外搜索关键词
                    for alias in aliases[:2]:  # 只使用前2个别名
                        search_keywords.append(f"{name} {alias}")
                    break
            
            # 原始歌手名也加入
            search_keywords.insert(1, f"{name} {artist}")
        
        # 返回第一个搜索关键词（主要）
        return search_keywords[0] if len(search_keywords) == 1 else search_keywords[1]

    def get_all_search_keywords(self) -> list[str]:
        """获取所有可能的搜索关键词（用于多次搜索）"""
        name = self._clean_name()
        artist = self._clean_artist()
        
        keywords = []
        if artist:
            keywords.append(f"{name} {artist}")
        
        # 添加别名
        for key, aliases in ARTIST_ALIASES.items():
            if key in self.artist or key in artist:
                for alias in aliases:
                    keywords.append(f"{name} {alias}")
                break
        
        if not keywords:
            keywords.append(name)
        
        return keywords[:3]  # 最多返回3个关键词


@dataclass
class BilibiliVideo:
    bvid: str
    title: str
    uploader: str
    duration: str = ""
    play_count: int = 0
    favorite_count: int = 0
    danmaku_count: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    is_official: bool = False
    is_verified: bool = False  # UP主是否认证（闪电标）
    quality_score: float = 0.0
    aid: int = 0  # 视频的数字ID，用于收藏夹API

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"


@dataclass
class MatchResult:
    song: KugouSong
    video: Optional[BilibiliVideo] = None
    score: float = 0.0
    keyword_score: float = 0.0
    quality_score: float = 0.0
    official_score: float = 0.0
    popularity_score: float = 0.0
    up_score: float = 0.0
    matched: bool = False
    manual_override: bool = False
    needs_review: bool = False
    has_selection: bool = False
    candidates: list = None  # type: ignore

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = []
        self.has_selection = self.matched


@dataclass
class BilibiliFavorite:
    fid: int
    title: str
    count: int = 0
    media_count: int = 0
