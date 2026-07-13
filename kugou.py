from __future__ import annotations

import re
import json
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from loguru import logger
from rich.console import Console

from models import KugouSong
from netx import Client, RetryConfig


class KugouScraper:
    BASE_URL = "https://m.kugou.com"
    SHARE_URL_PATTERN = re.compile(r"kugou\.com.*(?:zlist|plist|special|songlist|share)", re.IGNORECASE)

    def __init__(self, page=None):
        self.page = page
        retry = RetryConfig(max_attempts=3, base_backoff=0.25, max_backoff=3.0)
        self._netx = Client(
            timeout=20.0,
            retry=retry,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Referer": "https://m.kugou.com/",
            },
        )
        self._http = self._netx._http

    def scrape_playlist(self, url: str) -> list[KugouSong]:
        logger.debug(f"Scraping Kugou playlist: {url}")

        songs = self._try_http_scrape(url)

        if not songs and self.page:
            songs = self._try_playwright_scrape(url)

        return songs

    def _try_http_scrape(self, url: str) -> list[KugouSong]:
        songs: list[KugouSong] = []
        try:
            resp = self._netx.get(url)
            if resp is None:
                return songs
            final_url = str(resp.url)
            text = resp.text

            parsed = urlparse(final_url)
            qparams = parse_qs(parsed.query)
            specialid = qparams.get("specialid", [""])[0]
            global_specialid = qparams.get("global_specialid", [""])[0]

            playlist_id = specialid if specialid and specialid != "-2147483648" else ""
            if not playlist_id and global_specialid:
                playlist_id = global_specialid

            if playlist_id:
                songs = self._scrape_via_api(playlist_id)
                if songs:
                    return songs

            if not playlist_id:
                t_params = parse_qs(urlparse(url).query)
                sid = t_params.get("specialid", [""])[0]
                gsid = t_params.get("global_specialid", [""])[0]
                playlist_id = sid if sid and sid != "-2147483648" else gsid
                if playlist_id:
                    songs = self._scrape_via_api(playlist_id)
                    if songs:
                        return songs

            songs = self._extract_from_html(text)
            if songs:
                return songs

        except Exception as e:
            logger.warning(f"HTTP scrape failed: {e}")

        return songs

    def _try_playwright_scrape(self, url: str) -> list[KugouSong]:
        if not self.page:
            return []
        songs: list[KugouSong] = []
        seen = set()
        network_songs: list[KugouSong] = []

        def _add_song(name: str, artist: str):
            key = f"{name}|{artist}"
            if key not in seen:
                seen.add(key)
                s = KugouSong(name=name.strip(), artist=artist.strip() if artist else "")
                network_songs.append(s)

        def _extract_from_json(data) -> bool:
            if not isinstance(data, dict):
                return False
            added = False
            for key in ("info", "songs", "list", "songlist", "songList", "data"):
                items = data.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("songname", item.get("name", item.get("title", item.get("songName", ""))))
                    artist = item.get("singername", item.get("author", item.get("artist", item.get("singerName", ""))))
                    if name:
                        _add_song(str(name), str(artist) if artist else "")
                        added = True
                if added:
                    return True
            return False

        try:
            mobile_ua = (
                "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/111.0.0.0 Mobile Safari/537.36"
            )
            ctx = self.page.context
            new_page = ctx.new_page()

            new_page.on("response", lambda resp: self._on_ajax_response(resp, _extract_from_json))

            try:
                new_page.goto(url, wait_until="networkidle")
                new_page.wait_for_timeout(3000)

                expand_selectors = [
                    'button[class*="expand"]', 'a[class*="expand"]',
                    'button:has-text("展开")', 'button:has-text("查看全部")',
                    'button:has-text("全部")', '.show-all', '.showMore',
                    '[class*="show-all"]', '[class*="showMore"]',
                    '[class*="unfold"]', '[class*="open-all"]',
                ]
                for sel in expand_selectors:
                    try:
                        btn = new_page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            new_page.wait_for_timeout(2000)
                            break
                    except Exception:
                        pass

                last_scroll_height = 0
                stale_count = 0
                max_scrolls = 50

                for i in range(max_scrolls):
                    new_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    new_page.wait_for_timeout(600)

                    new_page.evaluate("window.scrollBy(0, -100)")
                    new_page.wait_for_timeout(200)
                    new_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    new_page.wait_for_timeout(600)

                    cur = new_page.evaluate("document.body.scrollHeight")
                    if cur == last_scroll_height:
                        stale_count += 1
                        new_page.wait_for_timeout(1000)
                        cur2 = new_page.evaluate("document.body.scrollHeight")
                        if cur2 == last_scroll_height and stale_count >= 3:
                            break
                    else:
                        stale_count = 0
                    last_scroll_height = cur

                    try:
                        btns = new_page.query_selector_all(
                            'button[class*="more"], a[class*="more"], [class*="load-more"], '
                            '[class*="loadMore"], [class*="next-page"], [class*="nextPage"], '
                            '.m-list-more, .list-more, [data-more]'
                        )
                        for btn in btns:
                            if btn and btn.is_visible():
                                btn.click()
                                new_page.wait_for_timeout(1500)
                    except Exception:
                        pass

                new_page.wait_for_timeout(2000)

                extracted = new_page.evaluate("""() => {
                    const results = [];
                    const seen = new Set();

                    const globals = ['songData', 'playlistData', 'listData', 'songsData', 'data',
                                     '__INITIAL_STATE__', '__NUXT__', '__NEXT_DATA__'];
                    for (let g of globals) {
                        try {
                            let obj = window[g];
                            if (!obj) continue;
                            if (typeof obj === 'string') obj = JSON.parse(obj);
                            function walk(o, depth) {
                                if (!o || typeof o !== 'object' || depth > 8) return;
                                if (Array.isArray(o) && o.length > 0 && typeof o[0] === 'object') {
                                    const first = o[0];
                                    if (first.songname || first.name || first.title || first.songName) {
                                        o.forEach(v => {
                                            const name = (v.songname || v.name || v.title || v.songName || v.FileName || '').toString().trim();
                                            const artist = (v.singername || v.author || v.artist || v.singerName || '').toString().trim();
                                            if (name && !seen.has(name + '|' + artist)) {
                                                results.push({ name, artist });
                                                seen.add(name + '|' + artist);
                                            }
                                        });
                                    }
                                }
                                const keys = ['info', 'songs', 'list', 'songlist', 'songList', 'data', 'playlist', 'tracks'];
                                for (let k of keys) {
                                    if (Array.isArray(o[k]) && o[k].length > 0) {
                                        walk(o[k], depth + 1);
                                    }
                                }
                                if (Array.isArray(o)) {
                                    o.forEach(v => walk(v, depth + 1));
                                } else {
                                    Object.values(o).forEach(v => walk(v, depth + 1));
                                }
                            }
                            walk(obj, 0);
                            if (results.length > 0) return results;
                        } catch(e) {}
                    }

                    const selectors = [
                        '.song-item', '.list_content li', '[class*="songItem"]', '[class*="song-item"]',
                        '.music-item', '.track-item', '[class*="trackItem"]', '[class*="musicItem"]',
                        'li[data-index]', 'li[data-songid]', '[class*="songRow"]', '[class*="song_row"]',
                        '.list-item', '[class*="listItem"]', '[class*="ListItem"]',
                    ];
                    const allItems = document.querySelectorAll(selectors.join(', '));

                    allItems.forEach(item => {
                        let name = '';
                        let artist = '';

                        const nameSels = ['[class*="song-name"]', '[class*="songName"]', '.songname',
                                          '.song_name', '[class*="songName"]', '[class*="title"]',
                                          '.name', '[class*="name"]', 'h3', 'h4'];
                        for (let ns of nameSels) {
                            const el = item.querySelector(ns);
                            if (el && el.innerText.trim()) {
                                name = el.innerText.trim();
                                break;
                            }
                        }

                        const artSels = ['[class*="singer"]', '[class*="artist"]', '.singername',
                                        '.singer_name', '[class*="author"]', '.artist',
                                        '[class*="singerName"]'];
                        for (let as of artSels) {
                            const el = item.querySelector(as);
                            if (el && el.innerText.trim()) {
                                artist = el.innerText.trim();
                                break;
                            }
                        }

                        if (!name) {
                            const text = item.innerText.trim();
                            if (text.includes(' - ')) {
                                const parts = text.split(' - ');
                                if (parts.length >= 2) {
                                    const p1 = parts[0].trim();
                                    const seps = ['、', ',', '&', '/', '，'];
                                    if (!seps.some(s => p1.includes(s)) && p1.length > 0 && p1.length < 100) {
                                        name = p1;
                                        artist = parts.slice(1).join(' - ').trim();
                                    }
                                }
                            }
                        }

                        if (name && name.length > 0 && name.length < 100) {
                            const seps = ['、', ',', '&', '/', '，'];
                            if (!seps.some(s => name.includes(s))) {
                                const key = name + '|' + (artist || '');
                                if (!seen.has(key)) {
                                    results.push({ name, artist: artist || '' });
                                    seen.add(key);
                                }
                            }
                        }
                    });

                    return results;
                }""")

                for item in extracted:
                    name = item.get("name", "").strip()
                    artist = item.get("artist", "").strip()
                    if name and not self._is_non_song_text(name):
                        _add_song(name, artist)

                all_songs = network_songs + [
                    KugouSong(name=name, artist=artist)
                    for name, artist in [
                        (item.get("name", "").strip(), item.get("artist", "").strip())
                        for item in extracted
                    ]
                    if name and not self._is_non_song_text(name)
                ]

                final_seen = set()
                for s in all_songs:
                    key = f"{s.name}|{s.artist}"
                    if key not in final_seen:
                        final_seen.add(key)
                        songs.append(s)

                songs = self._cleanup_phantom_entries(songs)
                logger.info(f"Playwright extracted {len(songs)} songs (network: {len(network_songs)}, DOM: {len(extracted)})")

            finally:
                new_page.close()

        except Exception as e:
            logger.error(f"Playwright scrape failed: {e}")

        return songs

    @staticmethod
    def _on_ajax_response(response, extractor) -> None:
        try:
            url = response.url
            if any(kw in url for kw in ("speciallist", "plist", "special/song", "songlist", "playlist")):
                data = response.json()
                extractor(data)
        except Exception:
            pass

    def _scrape_via_api(self, playlist_id: str) -> list[KugouSong]:
        songs: list[KugouSong] = []
        seen = set()

        api_endpoints = [
            ("https://mobileservice.kugou.com/api/v3/plist/speciallist", {"specialid": playlist_id, "pagesize": "200", "page": "1"}),
            ("https://mobileservice.kugou.com/api/v3/plist/list", {"specialid": playlist_id, "pagesize": "200", "page": "1"}),
            ("https://m.kugou.com/plist/list/{playlist_id}", None),
            ("https://wwwapi.kugou.com/playlist/detail/{playlist_id}", None),
            ("https://mobileservice.kugou.com/api/v3/special/song", {"specialid": playlist_id, "pagesize": "200", "page": "1"}),
        ]

        def extract_items(data: dict) -> list:
            if not isinstance(data, dict):
                return []
            info = data.get("data", data)
            if not isinstance(info, dict):
                return data if isinstance(data, list) else []
            for key in ("info", "songs", "list", "songlist", "songList", "data"):
                val = info.get(key)
                if isinstance(val, list) and val:
                    return val
                if isinstance(val, dict):
                    for subkey in ("info", "songs", "list", "songlist", "data"):
                        subval = val.get(subkey)
                        if isinstance(subval, list) and subval:
                            return subval
            return []

        for api_url, params in api_endpoints:
            try:
                url = api_url.replace("{playlist_id}", playlist_id)
                if params:
                    resp = self._netx.get(url, params=params)
                else:
                    resp = self._netx.get(url)
                if resp is None:
                    continue
                data = resp.json()

                items = extract_items(data)
                logger.debug(f"API {api_url}: extracted {len(items)} items from response")

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("songname", item.get("name", item.get("title", item.get("songName", ""))))
                    artist = item.get("singername", item.get("author", item.get("artist", item.get("singerName", ""))))
                    if not name:
                        continue
                    key = f"{name}|{artist}"
                    if key not in seen:
                        seen.add(key)
                        songs.append(KugouSong(
                            name=str(name).strip(),
                            artist=str(artist).strip() if artist else "",
                            album=item.get("album_name", item.get("albumname", item.get("album", ""))),
                            duration=self._format_duration(item.get("duration", 0)),
                            hash=item.get("hash", item.get("320hash", item.get("filehash", ""))),
                        ))

                if songs:
                    songs = self._cleanup_phantom_entries(songs)
                    logger.info(f"API returned {len(songs)} songs")
                    return songs
            except Exception as e:
                logger.debug(f"API {api_url} failed: {e}")
                continue

        logger.warning(f"API scraping failed for playlist {playlist_id}, no endpoint returned songs")
        return songs

    def _extract_from_html(self, html: str) -> list[KugouSong]:
        songs: list[KugouSong] = []

        for pattern in [
            r'var\s+songData\s*=\s*(\[.*?\]);',
            r'playlistData\s*=\s*(\{.*?\});',
            r'"songs"\s*:\s*(\[.*?\])',
        ]:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(1))
                    items = parsed if isinstance(parsed, list) else parsed.get("list", parsed.get("info", []))
                    for item in items:
                        song = KugouSong(
                            name=item.get("songname", item.get("name", item.get("title", ""))),
                            artist=item.get("singername", item.get("author", "")),
                        )
                        if song.name:
                            songs.append(song)
                    if songs:
                        return songs
                except (json.JSONDecodeError, ValueError):
                    pass

        return songs

    @staticmethod
    def _cleanup_phantom_entries(songs: list[KugouSong]) -> list[KugouSong]:
        if not songs:
            return songs
        artist_set = {s.artist.strip() for s in songs if s.artist.strip()}
        name_with_artist = {s.name for s in songs if s.artist.strip()}
        cleaned: list[KugouSong] = []
        seen = set()
        for s in songs:
            if s.name in artist_set:
                continue
            if not s.artist.strip() and s.name in name_with_artist:
                continue
            artist_seps = ['、', ',', '&', '/', '，']
            if any(sep in s.name for sep in artist_seps):
                continue
            key = f"{s.name}|{s.artist}"
            if key not in seen:
                seen.add(key)
                cleaned.append(s)
        return cleaned

    @staticmethod
    def _is_non_song_text(text: str) -> bool:
        skip = ["全部", "播放", "VIP", "收藏", "歌单", "分享", "下载", "评论", "首歌曲",
                "正在加载", "加载中", "Loading", "暂无", "没有更多", "已到底"]
        return any(s in text for s in skip)

    @staticmethod
    def _format_duration(seconds) -> str:
        try:
            s = int(seconds)
            return f"{s // 60}:{s % 60:02d}"
        except (ValueError, TypeError):
            return ""
