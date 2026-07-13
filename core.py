from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, IntPrompt, Confirm
from loguru import logger

from bilibili import BilibiliClient
from kugou import KugouScraper
from matcher import BilibiliMatcher
from manual_match import manual_match_song, interactive_review
from models import KugouSong, BilibiliVideo, MatchResult, BilibiliFavorite
from errors import Music2bbError, ErrorCategory, exit_for, ExitCode

console = Console()


def setup_logging(verbose: bool = False):
    level = "DEBUG" if verbose else "INFO"
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="{time:HH:mm:ss} | {level:<5} | {message}",
        colorize=False,
    )


def step_ensure_bilibili_login(client: BilibiliClient, qr_login: bool = True) -> bool:
    if client.load_cookies("bilibili"):
        if client.is_logged_in():
            return True

    console.print("\n[bold yellow]需要登录Bilibili账号[/bold yellow]")

    if qr_login:
        console.print("[bold cyan]扫码登录[/bold cyan]")
        console.print("正在生成二维码...")
        try:
            if client.qr_login():
                console.print("[bold green]扫码登录成功[/bold green]")
                return True
            else:
                console.print("[bold yellow]扫码登录失败/超时[/bold yellow]")
        except Exception as e:
            logger.warning(f"QR login error: {e}")
            console.print("[bold yellow]扫码登录异常[/bold yellow]")

    console.print("[bold red]请使用 GUI 模式扫码登录，或删除 .cookies 目录后重试[/bold red]")
    return False


def step_scrape_kugou(scraper: KugouScraper, url: str, browser_policy: str = "auto") -> list[KugouSong]:
    console.print(f"\n[bold cyan]正在解析歌单...[/bold cyan]")

    with console.status("加载歌单页面..."):
        songs = scraper.scrape_playlist(url)

    if not songs and browser_policy != "never":
        console.print("[dim]HTTP解析失败，尝试使用浏览器...[/dim]")
        try:
            from playwright.sync_api import sync_playwright
            with console.status("使用浏览器解析歌单..."):
                pw = sync_playwright().start()
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    user_agent="Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Mobile Safari/537.36",
                )
                pw_page = ctx.new_page()
                scraper_pw = KugouScraper(page=pw_page)
                songs = scraper_pw.scrape_playlist(url)
                ctx.close()
                browser.close()
                pw.stop()
        except Exception as e:
            logger.warning(f"浏览器解析失败: {e}")

    if not songs:
        if browser_policy == "always":
            console.print("[bold red]浏览器解析失败，无法提取歌曲[/bold red]")
        else:
            console.print("[dim]尝试手动输入歌曲列表（每行一首，格式：歌名 - 歌手，空行结束）[/dim]")
            songs = manual_input_songs()

    return songs


def manual_input_songs() -> list[KugouSong]:
    songs: list[KugouSong] = []
    console.print("[bold]请输入歌曲列表（每行一首，格式：歌名 - 歌手，输入空行结束）:[/bold]")
    while True:
        line = input().strip()
        if not line:
            break
        parts = line.split(" - ", 1)
        name = parts[0].strip()
        artist = parts[1].strip() if len(parts) > 1 else ""
        if name:
            songs.append(KugouSong(name=name, artist=artist))
    return songs


def _match_one_song(
    client: BilibiliClient,
    matcher: BilibiliMatcher,
    song: KugouSong,
    search_pages: int = 3,
) -> MatchResult:
    keyword = song.search_keyword_full
    videos: list[BilibiliVideo] = []

    for pg in range(1, search_pages + 1):
        results = client.search_videos(keyword, page=pg, page_size=20)
        videos.extend(results)

    if videos:
        top_matches = matcher.match(song, videos, top_k=1)
        if top_matches and top_matches[0].matched:
            return top_matches[0]

    alt_keywords = song.get_all_search_keywords()
    for alt_kw in alt_keywords:
        if alt_kw == keyword:
            continue
        alt_videos: list[BilibiliVideo] = []
        for pg in range(1, search_pages + 1):
            results = client.search_videos(alt_kw, page=pg, page_size=20)
            alt_videos.extend(results)
        if alt_videos:
            top_matches = matcher.match(song, alt_videos, top_k=1)
            if top_matches and top_matches[0].matched:
                return top_matches[0]

    return MatchResult(song=song, matched=False)


def step_match_songs(
    client: BilibiliClient,
    matcher: BilibiliMatcher,
    songs: list[KugouSong],
    top_k: int = 3,
    search_pages: int = 3,
    workers: int = 4,
) -> list[MatchResult]:
    all_results: list[MatchResult] = [MatchResult(song=s, matched=False) for s in songs]
    pending_idx: list[int] = list(range(len(songs)))
    total = len(songs)

    console.print(f"\n[bold cyan]正在匹配 {total} 首歌曲到Bilibili...[/bold cyan]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("匹配中...", total=total)
        completed = 0

        with ThreadPoolExecutor(max_workers=min(workers, total)) as executor:
            future_map: dict = {}
            batch = pending_idx[:workers]
            pending_idx = pending_idx[workers:]

            for idx in batch:
                future_map[executor.submit(_match_one_song, client, matcher, songs[idx], search_pages)] = idx

            while future_map:
                for future in as_completed(tuple(future_map.keys())):
                    idx = future_map.pop(future)
                    try:
                        result = future.result()
                    except Exception as e:
                        logger.error(f"Match error for {songs[idx].name}: {e}")
                        result = MatchResult(song=songs[idx], matched=False)

                    all_results[idx] = result
                    completed += 1

                    song_name = songs[idx].name[:20]
                    if result.matched and result.video:
                        progress.update(task, description=f"[{completed}/{total}] 匹配 {song_name} → {result.video.title[:25]}")
                    else:
                        progress.update(task, description=f"[{completed}/{total}] 未匹配 {song_name}")
                    progress.advance(task)

                    if pending_idx:
                        next_idx = pending_idx.pop(0)
                        future_map[executor.submit(_match_one_song, client, matcher, songs[next_idx], search_pages)] = next_idx

    return all_results


def step_select_favorites(client: BilibiliClient, favorite_selector: str = "") -> Optional[BilibiliFavorite]:
    console.print("\n[bold cyan]正在获取Bilibili收藏夹列表...[/bold cyan]")

    favs = client.get_favorite_lists()

    if not favs:
        console.print("[bold red]未找到收藏夹，请先在Bilibili创建一个收藏夹[/bold red]")
        return None

    if favorite_selector:
        try:
            fid = int(favorite_selector)
            for f in favs:
                if f.fid == fid:
                    console.print(f"已选择收藏夹: {f.title}")
                    return f
        except ValueError:
            for f in favs:
                if f.title == favorite_selector:
                    console.print(f"已选择收藏夹: {f.title}")
                    return f
        console.print(f"[bold yellow]未找到收藏夹「{favorite_selector}」，请手动选择[/bold yellow]")

    table = Table(title="Bilibili 收藏夹列表")
    table.add_column("序号", style="cyan", width=6)
    table.add_column("ID", style="dim", width=12)
    table.add_column("名称", style="white")
    table.add_column("内容数", style="green", width=8)

    for i, fav in enumerate(favs, 1):
        table.add_row(str(i), str(fav.fid), fav.title, str(fav.media_count))

    console.print(table)
    console.print("\n[bold]0. 新建收藏夹[/bold]")

    choice = IntPrompt.ask(
        "\n选择收藏夹序号",
        choices=[str(i) for i in range(0, len(favs) + 1)],
    )

    if choice == 0:
        return step_create_favorite(client)

    return favs[choice - 1]


def step_create_favorite(client: BilibiliClient) -> Optional[BilibiliFavorite]:
    console.print("\n[bold cyan]新建收藏夹[/bold cyan]")
    title = Prompt.ask("收藏夹名称")
    if not title.strip():
        console.print("[bold red]名称不能为空[/bold red]")
        return None
    intro = Prompt.ask("简介(可选)", default="")
    privacy = Confirm.ask("仅自己可见?", default=False)

    fav = client.create_favorite(title.strip(), intro, 1 if privacy else 0)
    if fav:
        console.print(f"[bold green]收藏夹「{fav.title}」创建成功 (id: {fav.fid})[/bold green]")
    else:
        console.print("[bold red]创建收藏夹失败[/bold red]")
    return fav


def display_match_results(results: list[MatchResult]):
    matched = [r for r in results if r.matched]
    unmatched = [r for r in results if not r.matched]

    console.print(f"\n[bold green]匹配成功: {len(matched)}/{len(results)}[/bold green]")

    if matched:
        table = Table(title="匹配结果", show_lines=True)
        table.add_column("序号", style="cyan", width=4)
        table.add_column("酷狗歌曲", style="white", width=30)
        table.add_column("Bilibili视频", style="yellow", width=40)
        table.add_column("UP主", style="blue", width=15)
        table.add_column("综合分", style="green", width=8)
        table.add_column("关键词", style="dim", width=6)
        table.add_column("音质", style="dim", width=6)
        table.add_column("官方", style="dim", width=6)
        table.add_column("热度", style="dim", width=6)

        for i, r in enumerate(matched, 1):
            v = r.video
            review_mark = " [yellow]*[/yellow]" if r.needs_review else ""
            table.add_row(
                str(i),
                f"{r.song.name} - {r.song.artist}{review_mark}",
                v.title[:40] if v else "",
                v.uploader[:15] if v else "",
                f"{r.score:.1f}",
                f"{r.keyword_score:.0f}",
                f"{r.quality_score:.0f}",
                f"{r.official_score:.0f}",
                f"{r.popularity_score:.0f}",
            )

        console.print(table)

    if unmatched:
        console.print(f"\n[bold red]未匹配 ({len(unmatched)} 首):[/bold red]")
        for r in unmatched:
            console.print(f"  - {r.song.name} - {r.song.artist}")


def step_add_to_favorites(
    client: BilibiliClient,
    results: list[MatchResult],
    fav: BilibiliFavorite,
) -> dict:
    matched = [r for r in results if r.matched and r.video]
    videos = [r.video for r in matched]

    if not videos:
        console.print("[bold red]没有可添加的视频[/bold red]")
        return {"success": [], "failed": []}

    console.print(f"\n[bold cyan]正在将 {len(videos)} 个视频添加到收藏夹「{fav.title}」...[/bold cyan]")

    result = client.add_to_favorites(videos, fav.fid)

    console.print(
        f"\n[bold green]成功: {len(result['success'])}[/bold green] | "
        f"[bold red]失败: {len(result['failed'])}[/bold red]"
    )

    if result["failed"]:
        for item in result["failed"]:
            console.print(f"  [red]✗ {item['bvid']}: {item['reason']}[/red]")

    return result


def run_convert(
    url: str,
    search_pages: int = 3,
    top_k: int = 3,
    workers: int = 4,
    favorite: str = "",
    yes: bool = False,
    verbose: bool = False,
    qr_login: bool = True,
    manual: bool = False,
    manual_review: bool = False,
    browser_policy: str = "auto",
    config_dir: str = "",
) -> int:
    setup_logging(verbose)

    console.print(Panel.fit(
        "[bold]在线歌单 → Bilibili 收藏夹[/bold]\n"
        "[dim]自动匹配歌曲到Bilibili视频并添加到收藏夹[/dim]",
        border_style="cyan",
    ))

    client = BilibiliClient()
    scraper = KugouScraper()
    matcher = BilibiliMatcher()

    try:
        if not step_ensure_bilibili_login(client, qr_login=qr_login):
            return ExitCode.AUTHENTICATION

        songs = step_scrape_kugou(scraper, url, browser_policy=browser_policy)
        if not songs:
            console.print("[bold red]没有获取到歌曲，退出[/bold red]")
            return ExitCode.EXTRACTION

        console.print(f"\n[bold green]获取到 {len(songs)} 首歌曲:[/bold green]")
        for i, s in enumerate(songs[:20], 1):
            console.print(f"  {i}. {s.name} - {s.artist}")
        if len(songs) > 20:
            console.print(f"  ... 还有 {len(songs) - 20} 首")

        if manual:
            console.print("\n[bold magenta]=== 完全手动匹配模式 ===[/bold magenta]")
            results = []
            for song in songs:
                result = manual_match_song(client, matcher, song)
                if result:
                    results.append(result)
                else:
                    results.append(MatchResult(song=song, matched=False))
        else:
            results = step_match_songs(client, matcher, songs, top_k=top_k, search_pages=search_pages, workers=workers)

            display_match_results(results)

            needs_review = any(r.needs_review for r in results)
            if manual_review or needs_review:
                if needs_review and not manual_review:
                    console.print("\n[bold yellow]部分匹配缺少歌手证据，建议审核[/bold yellow]")
                console.print("\n[bold magenta]=== 进入匹配审核模式 ===[/bold magenta]")
                results = interactive_review(client, matcher, results)
                display_match_results(results)

        matched_count = sum(1 for r in results if r.matched)
        if matched_count == 0:
            console.print("[bold red]没有匹配到任何歌曲，退出[/bold red]")
            return ExitCode.NO_MATCHES

        fav = step_select_favorites(client, favorite_selector=favorite)
        if not fav:
            return ExitCode.INVALID_INPUT

        if not yes:
            if not Confirm.ask(f"\n确认将 {matched_count} 个视频添加到收藏夹「{fav.title}」?"):
                console.print("[dim]已取消[/dim]")
                return ExitCode.SUCCESS

        step_add_to_favorites(client, results, fav)

        console.print("\n[bold green]完成[/bold green]")
        return ExitCode.SUCCESS

    except KeyboardInterrupt:
        console.print("\n[bold yellow]已中断[/bold yellow]")
        return ExitCode.CANCELLED
    except Exception as e:
        console.print(f"\n[bold red]错误: {e}[/bold red]")
        logger.exception(e)
        return exit_for(e)
    finally:
        client.close()
