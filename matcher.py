from __future__ import annotations

import re
import math
from pathlib import Path
from typing import Optional

from loguru import logger

from models import KugouSong, BilibiliVideo, MatchResult

# 配置文件路径
CONFIG_DIR = Path(__file__).parent
BLOCK_FILE = CONFIG_DIR / "b.txt"
WEIGHT_FILE = CONFIG_DIR / "w.txt"
WEIGHT_UP_FILE = CONFIG_DIR / "w-up.txt"


def load_keywords(filepath: Path) -> list[str]:
    """从文件加载关键词，每行一个"""
    if not filepath.exists():
        return []
    keywords = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        kw = line.strip()
        if kw and not kw.startswith("#"):
            keywords.append(kw)
    return keywords


# 从文件加载屏蔽关键词
BLOCK_KEYWORDS: list[str] = load_keywords(BLOCK_FILE)
if not BLOCK_KEYWORDS:
    BLOCK_KEYWORDS = ["翻唱", "伴奏", "cover", "教程", "乐谱"]
    logger.warning("b.txt not found, using default block keywords")

# 从文件加载加权关键词
WEIGHT_KEYWORDS: list[str] = load_keywords(WEIGHT_FILE)
if not WEIGHT_KEYWORDS:
    WEIGHT_KEYWORDS = ["官方", "official", "MV", "无损", "flac"]
    logger.warning("w.txt not found, using default weight keywords")

# 从文件加载UP主加权列表
WEIGHT_UP_LIST: list[str] = load_keywords(WEIGHT_UP_FILE)
if not WEIGHT_UP_LIST:
    WEIGHT_UP_LIST = []
    logger.warning("w-up.txt not found, no UP weighting")

# 加权分数
WEIGHT_SCORE: float = 15.0  # 每个标题关键词匹配的分数
WEIGHT_UP_SCORE: float = 30.0  # UP主完全匹配加权
WEIGHT_VERIFIED_SCORE: float = 20.0  # 认证UP主加权

OFFICIAL_KEYWORDS: list[str] = [
    "官方", "official", "Official", "OFFICIAL",
    "官方MV", "OfficialMV", "官方mv",
]

OFFICIAL_UP_PATTERNS: list[str] = [
    "官方", "Official", "Music", "Records", "Entertainment",
    "音乐", "唱片", "工作室",
]


class BilibiliMatcher:
    def __init__(self, weights: Optional[dict[str, float]] = None):
        self.weights = weights or {
            "keyword": 40.0,
            "quality": 25.0,
            "official": 20.0,
            "popularity": 15.0,
        }
        self._block_count = 0  # 统计屏蔽数量

    def compute_keyword_score(self, song: KugouSong, video: BilibiliVideo) -> float:
        title = video.title.lower()
        song_name = song.name.lower().strip()
        artist_name = song.artist.lower().strip()

        if not song_name:
            return 0.0

        song_name_clean = re.sub(r"[()（）\[\]【】\s]", "", song_name)
        artist_clean = re.sub(r"[()（）\[\]【】\s]", "", artist_name)

        title_clean = re.sub(r"[()（）\[\]【】\s]", "", title)
        title_words = set(re.findall(r"\w+", title))

        name_in_title = song_name_clean in title_clean
        artist_in_title = artist_clean in title_clean if artist_clean else True

        if name_in_title and artist_in_title:
            score = 100.0
        elif name_in_title:
            score = 70.0
        elif self._fuzzy_contains(title, song_name):
            score = 50.0
        else:
            song_words = set(re.findall(r"\w+", song_name))
            if song_words:
                overlap = len(song_words & title_words) / len(song_words)
                score = overlap * 40.0
            else:
                score = 0.0

        if artist_clean and not artist_in_title:
            artist_words = set(re.findall(r"\w+", artist_clean))
            if artist_words:
                art_overlap = len(artist_words & title_words)
                if art_overlap >= len(artist_words) * 0.5:
                    score = max(score, min(score + 15.0, 85.0))

        song_words_all = set(re.findall(r"\w+", song_name + " " + artist_name))
        if song_words_all:
            word_overlap = len(song_words_all & title_words) / len(song_words_all)
            score = max(score, word_overlap * 80.0)

        return score

    def compute_quality_score(self, video: BilibiliVideo) -> float:
        """计算质量分数，匹配多少个关键词就加多少倍分数"""
        text = (video.title + " " + video.description + " " + " ".join(video.tags)).lower()
        score = 0.0
        
        # 从w.txt加载的加权关键词，匹配多少个就加多少倍
        for kw in WEIGHT_KEYWORDS:
            if kw.lower() in text:
                score += WEIGHT_SCORE
        
        video.quality_score = score
        return score

    def compute_official_score(self, video: BilibiliVideo) -> float:
        text = (video.title + " " + video.uploader).lower()
        score = 0.0

        for kw in OFFICIAL_KEYWORDS:
            if kw.lower() in text:
                score += 20.0
                break

        for pattern in OFFICIAL_UP_PATTERNS:
            if pattern.lower() in video.uploader.lower():
                score += 15.0
                break

        if video.is_official:
            score = max(score, 25.0)

        return min(score, 30.0)

    def compute_popularity_score(self, video: BilibiliVideo) -> float:
        """计算热度分数，收藏量/播放量多的优先"""
        score = 0.0
        
        # 播放量加分（log计算，上限30分）
        if video.play_count > 0:
            play_score = min(30.0, math.log10(max(video.play_count, 1)) * 5.0)
            score += play_score
        
        # 收藏量加分（log计算，上限25分）
        if video.favorite_count > 0:
            fav_score = min(25.0, math.log10(max(video.favorite_count, 1)) * 5.0)
            score += fav_score
        
        # 收藏率加分（收藏量/播放量 > 10% 表示高质量）
        if video.play_count > 0 and video.favorite_count > 0:
            fav_rate = video.favorite_count / video.play_count
            if fav_rate > 0.1:  # 收藏率超过10%
                score += min(15.0, fav_rate * 50)  # 最高再加15分
        
        return score

    def compute_up_score(self, video: BilibiliVideo) -> float:
        """计算UP主加权分数"""
        score = 0.0
        
        # UP主完全匹配加权
        if video.uploader in WEIGHT_UP_LIST:
            score += WEIGHT_UP_SCORE
        
        # 认证UP主加权（闪电标）
        if video.is_verified:
            score += WEIGHT_VERIFIED_SCORE
        
        return score

    def _is_blocked(self, video: BilibiliVideo) -> bool:
        """检查视频是否包含屏蔽关键词（智能过滤）"""
        text = (video.title + " " + video.description).lower()
        
        for kw in BLOCK_KEYWORDS:
            kw_lower = kw.lower()
            
            # 单字关键词需要更严格的匹配（前后要有分隔）
            if len(kw) == 1:
                # 检查关键词是否作为独立词出现（前后有空格/括号/标点）
                pattern = r'(?:^|[^\w])' + kw_lower + r'(?:$|[^\w])'
                if re.search(pattern, text):
                    self._block_count += 1
                    return True
            else:
                # 多字关键词直接匹配
                if kw_lower in text:
                    self._block_count += 1
                    return True
        
        return False

    def _has_artist_evidence(self, song: KugouSong, video: BilibiliVideo) -> bool:
        if not song.artist.strip():
            return True
        artist_clean = re.sub(r"[()（）\[\]【】\s]", "", song.artist.lower().strip())
        if not artist_clean:
            return True
        title_clean = re.sub(r"[()（）\[\]【】\s]", "", video.title.lower())
        uploader_clean = re.sub(r"[()（）\[\]【】\s]", "", video.uploader.lower())

        if artist_clean in title_clean:
            return True
        if artist_clean in uploader_clean:
            return True

        artist_words = set(re.findall(r"\w+", artist_clean))
        title_words = set(re.findall(r"\w+", title_clean))
        uploader_words = set(re.findall(r"\w+", uploader_clean))

        if artist_words:
            title_overlap = len(artist_words & title_words)
            uploader_overlap = len(artist_words & uploader_words)
            if title_overlap >= len(artist_words) * 0.5:
                return True
            if uploader_overlap >= len(artist_words) * 0.5:
                return True

        return False

    def match(self, song: KugouSong, videos: list[BilibiliVideo], top_k: int = 1) -> list[MatchResult]:
        results: list[MatchResult] = []
        self._block_count = 0

        for video in videos:
            # 过滤屏蔽关键词的视频
            if self._is_blocked(video):
                continue

            kw = self.compute_keyword_score(song, video)
            ql = self.compute_quality_score(video)
            of = self.compute_official_score(video)
            po = self.compute_popularity_score(video)
            up = self.compute_up_score(video)

            total = (
                kw * self.weights["keyword"] / 100.0
                + ql * self.weights["quality"] / 100.0
                + of * self.weights["official"] / 100.0
                + po * self.weights["popularity"] / 100.0
                + up  # UP主加权直接加到总分
            )

            matched = kw >= 20.0
            needs_review = False
            if matched and not self._has_artist_evidence(song, video):
                needs_review = True

            results.append(
                MatchResult(
                    song=song,
                    video=video,
                    score=total,
                    keyword_score=kw,
                    quality_score=ql,
                    official_score=of,
                    popularity_score=po,
                    up_score=up,
                    matched=matched,
                    needs_review=needs_review,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        
        if self._block_count > 0:
            logger.debug(f"Blocked {self._block_count} videos for '{song.search_keyword}'")
        
        return results[:top_k]

    @staticmethod
    def _fuzzy_contains(text: str, target: str) -> bool:
        t_clean = re.sub(r"[()（）\[\]【】\s]", "", text)
        s_clean = re.sub(r"[()（）\[\]【】\s]", "", target)
        if len(s_clean) < 2:
            return s_clean in t_clean
        window = len(s_clean)
        for i in range(len(t_clean) - window + 1):
            matches = sum(a == b for a, b in zip(t_clean[i : i + window], s_clean))
            if matches / len(s_clean) >= 0.8:
                return True
        return False