from __future__ import annotations

import argparse
import sys
import os

from errors import ExitCode, exit_for
from config import load_config, Config, DEFAULT_BLOCK_KEYWORDS

VERSION = "music2bb 0.1.0"


def _browser_download_size(status_bytes: int = 0) -> str:
    if status_bytes <= 0:
        return "约 150–270 MB"
    mb = (status_bytes + 999_999) // 1_000_000
    return f"约 {mb} MB"


def _cmd_convert(args) -> int:
    from core import run_convert

    qr_login = getattr(args, "qr_login", True)
    if getattr(args, "no_qr_login", False):
        qr_login = False

    browser_policy = getattr(args, "browser", "auto")
    if browser_policy not in ("auto", "never", "always"):
        print("错误: --browser 必须是 auto、never 或 always", file=sys.stderr)
        return ExitCode.INVALID_INPUT

    return run_convert(
        url=args.url,
        search_pages=args.search_pages,
        top_k=args.top_k,
        workers=args.workers,
        favorite=args.favorite,
        yes=args.yes,
        verbose=args.verbose,
        qr_login=qr_login,
        manual=args.manual,
        manual_review=args.manual_review,
        browser_policy=browser_policy,
        config_dir=args.config_dir,
    )


def _cmd_login(args) -> int:
    from bilibili import BilibiliClient

    qr_login = getattr(args, "qr_login", True)
    if getattr(args, "no_qr_login", False):
        qr_login = False

    client = BilibiliClient()
    try:
        if client.load_cookies("bilibili") and client.is_logged_in():
            resp = client._api_get(client.NAV_API)
            uname = ""
            if resp and resp.get("code") == 0:
                uname = resp.get("data", {}).get("uname", "")
            print(f"登录成功: {uname}")
            return ExitCode.SUCCESS

        if qr_login:
            print("正在生成二维码...")
            if client.qr_login():
                uname = ""
                resp = client._api_get(client.NAV_API)
                if resp and resp.get("code") == 0:
                    uname = resp.get("data", {}).get("uname", "")
                print(f"登录成功: {uname}")
                return ExitCode.SUCCESS

        print("登录失败", file=sys.stderr)
        return ExitCode.AUTHENTICATION
    except KeyboardInterrupt:
        return ExitCode.CANCELLED
    finally:
        client.close()


def _cmd_favorites(args) -> int:
    from bilibili import BilibiliClient

    client = BilibiliClient()
    try:
        if not client.load_cookies("bilibili") or not client.is_logged_in():
            print("登录失败: 未登录", file=sys.stderr)
            return ExitCode.AUTHENTICATION

        sub = args.favorites_sub
        if sub == "list":
            favs = client.get_favorite_lists()
            if not favs:
                print("收藏夹为空")
            for f in favs:
                print(f"{f.fid}\t{f.title}\t{f.media_count}")
            return ExitCode.SUCCESS

        elif sub == "create":
            title = getattr(args, "name", "").strip()
            if not title:
                print("用法: python main.py favorites create <name> [--intro TEXT] [--private]", file=sys.stderr)
                return ExitCode.INVALID_INPUT
            intro = getattr(args, "intro", "")
            private = 1 if getattr(args, "private", False) else 0
            fav = client.create_favorite(title, intro, private)
            if fav:
                print(f"{fav.fid}\t{fav.title}")
                return ExitCode.SUCCESS
            else:
                print("创建收藏夹失败", file=sys.stderr)
                return ExitCode.INTERNAL
    except KeyboardInterrupt:
        return ExitCode.CANCELLED
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return exit_for(e)
    finally:
        client.close()


def _cmd_browser(args) -> int:
    sub = args.browser_sub

    if sub == "status":
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            try:
                browsers = list(pw.chromium.executable_path or [])
                if pw.chromium.executable_path:
                    print(f"installed\tpath={pw.chromium.executable_path}")
                else:
                    print("not installed")
            finally:
                pw.stop()
        except ImportError:
            print("playwright 未安装", file=sys.stderr)
            return ExitCode.EXTRACTION
        except Exception as e:
            print(f"not installed\terror={e}", file=sys.stderr)
        return ExitCode.SUCCESS

    elif sub == "install":
        print("正在安装 Chromium 浏览器...")
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            try:
                pw.chromium.launch(headless=True).close()
                print(f"installed\tpath={pw.chromium.executable_path}")
            finally:
                pw.stop()
        except ImportError:
            print("playwright 未安装，请执行: pip install playwright && playwright install chromium", file=sys.stderr)
            return ExitCode.EXTRACTION
        except Exception as e:
            print(f"安装失败: {e}", file=sys.stderr)
            return ExitCode.EXTRACTION
        return ExitCode.SUCCESS

    elif sub == "clear":
        import shutil
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            try:
                cache_dir = os.path.join(os.path.dirname(pw.chromium.executable_path or ""), "..")
                if os.path.exists(cache_dir) and "playwright" in cache_dir.lower():
                    shutil.rmtree(cache_dir, ignore_errors=True)
            finally:
                pw.stop()
        except Exception:
            pass
        print("cleared")
        return ExitCode.SUCCESS

    return ExitCode.INVALID_INPUT


def main():
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="在线歌单 → Bilibili 收藏夹",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    convert_parser = subparsers.add_parser("convert", help="转换歌单到收藏夹")
    convert_parser.add_argument("url", help="歌单链接")
    convert_parser.add_argument("--search-pages", type=int, default=3, help="每首歌曲搜索页数（默认3）")
    convert_parser.add_argument("--top-k", type=int, default=3, help="保留候选数量（默认3）")
    convert_parser.add_argument("--workers", type=int, default=4, help="并发匹配数量（默认4）")
    convert_parser.add_argument("--favorite", default="", help="收藏夹 ID 或完整名称")
    convert_parser.add_argument("--yes", action="store_true", help="跳过确认")
    convert_parser.add_argument("--browser", default="auto", choices=["auto", "never", "always"], help="浏览器策略（默认auto）")
    convert_parser.add_argument("--config-dir", default="", help="配置目录")
    convert_parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    convert_parser.add_argument("--manual", action="store_true", help="完全手动匹配")
    convert_parser.add_argument("--manual-review", action="store_true", help="手动审核自动匹配")
    convert_parser.add_argument("--qr-login", action="store_true", default=True, help="允许扫码登录")
    convert_parser.add_argument("--no-qr-login", action="store_true", help="禁止扫码登录")
    convert_parser.set_defaults(func=_cmd_convert)

    login_parser = subparsers.add_parser("login", help="登录 Bilibili")
    login_parser.add_argument("--qr-login", action="store_true", default=True, help="扫码登录")
    login_parser.add_argument("--no-qr-login", action="store_true", help="禁止扫码登录")
    login_parser.set_defaults(func=_cmd_login)

    fav_parser = subparsers.add_parser("favorites", help="管理收藏夹")
    fav_sub = fav_parser.add_subparsers(dest="favorites_sub")
    fav_list = fav_sub.add_parser("list", help="列出收藏夹")
    fav_list.set_defaults(func=_cmd_favorites)
    fav_create = fav_sub.add_parser("create", help="创建收藏夹")
    fav_create.add_argument("name", help="收藏夹名称")
    fav_create.add_argument("--intro", default="", help="简介")
    fav_create.add_argument("--private", action="store_true", help="仅自己可见")
    fav_create.set_defaults(func=_cmd_favorites)

    browser_parser = subparsers.add_parser("browser", help="管理浏览器")
    browser_sub = browser_parser.add_subparsers(dest="browser_sub")
    b_install = browser_sub.add_parser("install", help="安装浏览器")
    b_install.set_defaults(func=_cmd_browser)
    b_status = browser_sub.add_parser("status", help="浏览器状态")
    b_status.set_defaults(func=_cmd_browser)
    b_clear = browser_sub.add_parser("clear", help="清理浏览器缓存")
    b_clear.set_defaults(func=_cmd_browser)

    version_parser = subparsers.add_parser("version", help="显示版本")

    gui_parser = subparsers.add_parser("gui", help="启动 GUI 界面")

    help_lines = [
        "",
        "在线歌单 → Bilibili 收藏夹",
        "",
        "用法:",
        "  python main.py convert <playlist-url> [options]",
        "  python main.py login [--no-qr-login]",
        "  python main.py favorites list",
        "  python main.py favorites create <name> [--intro TEXT] [--private]",
        "  python main.py browser install|status|clear",
        "  python main.py version",
        "  python main.py gui",
    ]
    parser.epilog = "\n".join(help_lines)

    args = parser.parse_args()

    if args.command == "gui":
        from gui import run_gui
        run_gui()
        return

    if args.command == "version":
        print(VERSION)
        return

    if args.command is None:
        parser.print_help()
        sys.exit(ExitCode.INVALID_INPUT)

    if hasattr(args, "func"):
        exit_code = args.func(args)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
