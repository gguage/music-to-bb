from __future__ import annotations

import hashlib
import json
import time
import re
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import urlencode

import httpx
import qrcode
from loguru import logger
from PIL import Image

from models import BilibiliVideo, BilibiliFavorite
from netx import Client, TokenBucketLimiter, RetryConfig

COOKIE_DIR = Path(__file__).parent / ".cookies"
COOKIE_DIR.mkdir(exist_ok=True)

WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in WBI_MIXIN_TABLE)[:32]


def _sign_wbi_params(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = _get_mixin_key(img_key + sub_key)
    wts = int(time.time())
    params["wts"] = wts
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        v_str = re.sub(r"[^\w!\'()*_.\-]", "", str(v))
        filtered[k] = v_str
    query = urlencode(filtered)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = wbi_sign
    return filtered


class BilibiliClient:
    SEARCH_API = "https://api.bilibili.com/x/web-interface/search/all/v2"
    FAV_LIST_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
    FAV_ADD_API = "https://api.bilibili.com/x/v3/fav/resource/deal"
    FAV_CREATE_API = "https://api.bilibili.com/x/v3/fav/folder/add"
    NAV_API = "https://api.bilibili.com/x/web-interface/nav"
    VIDEO_DETAIL_API = "https://api.bilibili.com/x/web-interface/view"
    QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

    def __init__(self, rate_per_second: float = 0, config_dir: str = ""):
        retry = RetryConfig(max_attempts=3, base_backoff=0.25, max_backoff=3.0)
        limiter = TokenBucketLimiter(rate_per_second) if rate_per_second > 0 else None
        self._netx = Client(
            timeout=20.0,
            limiter=limiter,
            retry=retry,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            },
        )
        self._http = self._netx._http
        self._config_dir = config_dir
        self._wbi_img_key: Optional[str] = None
        self._wbi_sub_key: Optional[str] = None
        self._wbi_keys_ts: float = 0
        self._fingerprint_ready: bool = False
        # 搜索缓存：避免重复搜索相同关键词
        self._search_cache: dict[str, list[BilibiliVideo]] = {}
        self._cache_max_size: int = 100

    def _ensure_fingerprint(self):
        if self._fingerprint_ready:
            return
        try:
            has_buvid = any(c.name == "buvid3" for c in self._http.cookies.jar)
            if not has_buvid:
                self._http.get("https://www.bilibili.com/", follow_redirects=True)
                logger.debug("Visited bilibili.com to get fingerprint cookies")
                self.save_cookies()
            self._fingerprint_ready = True
        except Exception as e:
            logger.warning(f"Fingerprint init failed: {e}")

    def _cookie_path(self, name: str = "bilibili") -> Path:
        if self._config_dir:
            return Path(self._config_dir) / "cookies" / f"{name}.json"
        return COOKIE_DIR / f"{name}.json"

    def save_cookies(self, name: str = "bilibili"):
        try:
            cookies = []
            for c in self._http.cookies.jar:
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                })
            path = self._cookie_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"Cookies saved to {path} ({len(cookies)} items)")
        except Exception as e:
            logger.warning(f"Save cookies failed: {e}")

    def load_cookies(self, name: str = "bilibili") -> bool:
        path = self._cookie_path(name)
        if not path.exists():
            logger.warning(f"No saved cookies for {name}")
            return False
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
            for c in cookies:
                domain = c.get("domain", ".bilibili.com")
                self._http.cookies.set(
                    c["name"], c["value"],
                    domain=domain,
                    path=c.get("path", "/"),
                )
            logger.info(f"Cookies loaded from {path} ({len(cookies)} items)")
            self._fingerprint_ready = any(c.get("name") == "buvid3" for c in cookies)
            return True
        except Exception as e:
            logger.warning(f"Load cookies failed: {e}")
            return False

    def has_cookies(self, name: str = "bilibili") -> bool:
        return self._cookie_path(name).exists()

    def qr_login(self, on_qr_image: Optional[Callable[[Image.Image], None]] = None) -> bool:
        logger.info("Starting QR code login...")

        data = self._api_get(self.QR_GENERATE_API)
        if not data or data.get("code") != 0:
            logger.error(f"QR generate failed: {data}")
            return False

        qr_data = data.get("data", {})
        qr_url = qr_data.get("url", "")
        qrcode_key = qr_data.get("qrcode_key", "")

        if not qr_url or not qrcode_key:
            logger.error("No QR url or key in response")
            return False

        img = self._generate_qr_image(qr_url)

        if on_qr_image:
            on_qr_image(img)
        else:
            self._print_qr_terminal(qr_url)

        logger.info("Waiting for QR scan...")

        start = time.time()
        timeout = 180
        while time.time() - start < timeout:
            poll_data = self._api_get(self.QR_POLL_API, {"qrcode_key": qrcode_key})
            if poll_data:
                code = poll_data.get("data", {}).get("code", -1)
                if code == 0:
                    logger.info("QR login successful!")
                    cookie_raw = poll_data.get("data", {}).get("cookie", "")
                    self._apply_login_cookies(cookie_raw)
                    self.save_cookies()
                    return True
                elif code == 86038:
                    logger.warning("QR code expired")
                    return False
                elif code == 86090:
                    logger.info("QR scanned, waiting for confirmation...")
                elif code == 86101:
                    pass

            time.sleep(2)

        logger.error("QR login timeout")
        return False

    def _generate_qr_image(self, url: str) -> Image.Image:
        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white")

    def _print_qr_terminal(self, url: str):
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)

    def _apply_login_cookies(self, cookie_str: str):
        if not cookie_str:
            return
        count = 0
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            self._http.cookies.set(name.strip(), value.strip(), domain=".bilibili.com")
            count += 1
        logger.info(f"Applied {count} login cookies from QR response")

        resp_set = self._http.cookies.jar
        logger.info(f"httpx cookie jar now has {len(resp_set)} total cookies")

    def _refresh_wbi_keys(self):
        if self._wbi_img_key and (time.time() - self._wbi_keys_ts < 600):
            return
        try:
            resp = self._api_get(self.NAV_API)
            if resp and resp.get("code") == 0:
                wbi_img = resp.get("data", {}).get("wbi_img", {})
                self._wbi_img_key = (
                    wbi_img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
                )
                self._wbi_sub_key = (
                    wbi_img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
                )
                self._wbi_keys_ts = time.time()
                logger.debug(f"WBI keys refreshed: {self._wbi_img_key[:10]}...")
        except Exception as e:
            logger.warning(f"WBI keys refresh failed: {e}")

    def is_logged_in(self) -> bool:
        try:
            resp = self._api_get(self.NAV_API)
            if resp and resp.get("code") == 0:
                uname = resp.get("data", {}).get("uname", "")
                if uname:
                    logger.info(f"Logged in as: {uname}")
                    return True
            else:
                logger.debug(f"Login check response: code={resp.get('code') if resp else 'None'}")
        except Exception as e:
            logger.warning(f"Login check failed: {e}")
        return False

    def search_videos(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        search_type: int = 1,
        order: str = "totalrank",
    ) -> list[BilibiliVideo]:
        videos: list[BilibiliVideo] = []

        # 检查缓存
        cache_key = f"{keyword}:{page}"
        if cache_key in self._search_cache:
            logger.debug(f"Using cached results for '{keyword}' page {page}")
            return self._search_cache[cache_key]

        params = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
        }

        try:
            self._ensure_fingerprint()

            resp = self._netx.get(self.SEARCH_API, params=params)
            
            data = resp.json() if resp and resp.status_code == 200 else None
            if not data or data.get("code") != 0:
                logger.warning(f"Search failed for '{keyword}': code={data.get('code') if data else 'None'} msg={data.get('message','') if data else ''}")
                return videos

            result_blocks = data.get("data", {}).get("result", [])
            video_items = []
            for block in result_blocks:
                if isinstance(block, dict) and block.get("result_type") == "video":
                    video_items = block.get("data", [])
                    break

            if not video_items:
                for block in result_blocks:
                    if isinstance(block, dict) and block.get("bvid"):
                        video_items.append(block)

            for item in video_items:
                title = re.sub(r"<.*?>", "", item.get("title", ""))
                # 检查认证状态（闪电标）
                owner = item.get("owner", {}) or {}
                is_verified = owner.get("verify_type", 0) > 0 or item.get("is_verify", False)
                
                video = BilibiliVideo(
                    bvid=item.get("bvid", ""),
                    title=title,
                    uploader=item.get("author", ""),
                    duration=item.get("duration", ""),
                    play_count=item.get("play", 0),
                    favorite_count=item.get("favorites", 0),
                    danmaku_count=item.get("video_review", 0),
                    description=item.get("description", ""),
                    tags=item.get("tag", "").split(",") if item.get("tag") else [],
                    is_official=self._check_official(item),
                    is_verified=is_verified,
                    aid=item.get("aid", 0),  # 搜索API可能返回aid
                )
                if video.bvid:
                    videos.append(video)

            logger.debug(f"Search '{keyword}': {len(videos)} results")

            # 缓存结果（限制缓存大小）
            if len(self._search_cache) >= self._cache_max_size:
                # 删除最旧的缓存项
                oldest_key = next(iter(self._search_cache))
                del self._search_cache[oldest_key]
            self._search_cache[cache_key] = videos

        except Exception as e:
            logger.error(f"Search API error: {e}")

        return videos

    def get_video_detail(self, bvid: str) -> Optional[BilibiliVideo]:
        try:
            data = self._api_get(self.VIDEO_DETAIL_API, {"bvid": bvid})
            if not data or data.get("code") != 0:
                return None

            info = data.get("data", {})
            stat = info.get("stat", {})

            return BilibiliVideo(
                bvid=bvid,
                aid=info.get("aid", 0),  # 从API获取aid
                title=info.get("title", ""),
                uploader=info.get("owner", {}).get("name", ""),
                duration=self._format_duration(info.get("duration", 0)),
                play_count=stat.get("view", 0),
                favorite_count=stat.get("favorite", 0),
                danmaku_count=stat.get("danmaku", 0),
                description=info.get("desc", ""),
                tags=[],
                is_official=info.get("is_cooperation", False) or info.get("is_stein_gate", False),
            )
        except Exception as e:
            logger.warning(f"Get video detail failed for {bvid}: {e}")
            return None

    def get_favorite_lists(self) -> list[BilibiliFavorite]:
        favs: list[BilibiliFavorite] = []
        try:
            nav = self._api_get(self.NAV_API)
            mid = ""
            if nav and nav.get("code") == 0:
                mid = str(nav.get("data", {}).get("mid", ""))

            params = {"up_mid": mid} if mid else {}
            data = self._api_get(self.FAV_LIST_API, params)
            if not data or data.get("code") != 0:
                logger.error(f"Get favorites failed: code={data.get('code') if data else 'None'} msg={data.get('message','') if data else ''}")
                return favs

            items = data.get("data", {}).get("list", [])
            if not items:
                items = data.get("data", {}).get("count", 0) and data.get("data", {}).get("list", [])

            for item in items:
                favs.append(
                    BilibiliFavorite(
                        fid=item.get("id", 0),
                        title=item.get("title", ""),
                        count=item.get("media_count", 0),
                        media_count=item.get("media_count", 0),
                    )
                )

            logger.info(f"Found {len(favs)} favorite lists")

        except Exception as e:
            logger.error(f"Get favorites error: {e}")

        return favs

    def _get_csrf(self) -> str:
        for cookie in self._http.cookies.jar:
            if cookie.name == "bili_jct":
                return cookie.value
        return ""

    def create_favorite(self, title: str, intro: str = "", privacy: int = 0) -> Optional[BilibiliFavorite]:
        csrf = self._get_csrf()
        if not csrf:
            logger.error("No csrf token (bili_jct) found, cannot create favorite")
            return None

        payload = {
            "title": title,
            "intro": intro,
            "privacy": privacy,
            "csrf": csrf,
        }

        try:
            data = self._api_post(self.FAV_CREATE_API, payload)
            if not data or data.get("code") != 0:
                msg = data.get("message", "Unknown error") if data else "No response"
                logger.error(f"Create favorite failed: {msg}")
                return None

            new_id = data.get("data", {}).get("id", 0)
            logger.info(f"Created favorite folder '{title}' (id: {new_id})")
            return BilibiliFavorite(
                fid=new_id,
                title=title,
                count=0,
                media_count=0,
            )
        except Exception as e:
            logger.error(f"Create favorite error: {e}")
            return None

    def add_to_favorites(self, videos: list[BilibiliVideo], fav_id: int) -> dict:
        results = {"success": [], "failed": []}
        csrf = self._get_csrf()

        # 刷新WBI密钥
        self._refresh_wbi_keys()

        for video in videos:
            try:
                bvid = video.bvid
                aid = video.aid
                
                # 如果没有aid，尝试从API获取
                if aid == 0:
                    detail = self.get_video_detail(bvid)
                    if detail and detail.aid:
                        aid = detail.aid
                        video.aid = aid  # 更新对象
                    else:
                        results["failed"].append({"bvid": bvid, "reason": "无法获取aid"})
                        logger.warning(f"Cannot get aid for {bvid}")
                        continue

                # 使用WBI签名的参数
                params = {
                    "rid": aid,
                    "type": 2,
                    "add_media_ids": str(fav_id),
                    "del_media_ids": "",
                    "csrf": csrf,
                }
                
                # 添加WBI签名
                if self._wbi_img_key and self._wbi_sub_key:
                    params = _sign_wbi_params(params, self._wbi_img_key, self._wbi_sub_key)

                resp = self._api_post(self.FAV_ADD_API, params)

                if resp and resp.get("code") == 0:
                    results["success"].append(bvid)
                    logger.info(f"Added {bvid} to favorites")
                else:
                    msg = resp.get("message", "Unknown error") if resp else "No response"
                    code = resp.get("code", "unknown") if resp else "no_response"
                    results["failed"].append({"bvid": bvid, "reason": f"{msg} (code:{code})"})
                    logger.warning(f"Failed to add {bvid}: {msg}")

                time.sleep(0.15)  # 进一步减少延迟

            except Exception as e:
                results["failed"].append({"bvid": video.bvid, "reason": str(e)})
                logger.error(f"Error adding {video.bvid}: {e}")

        return results

    def _api_get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        try:
            resp = self._http.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"API GET failed ({url}): {e}")
            return None

    def _api_post(self, url: str, data: dict) -> Optional[dict]:
        try:
            resp = self._http.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"API POST failed ({url}): {e}")
            return None

    @staticmethod
    def _check_official(item: dict) -> bool:
        text = (item.get("title", "") + " " + item.get("author", "")).lower()
        for kw in ["官方", "official"]:
            if kw in text:
                return True
        return False

    @staticmethod
    def _format_duration(seconds) -> str:
        try:
            s = int(seconds)
            return f"{s // 60}:{s % 60:02d}"
        except (ValueError, TypeError):
            return ""

    def close(self):
        self._netx.close()
