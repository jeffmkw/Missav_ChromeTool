# -*- coding: utf-8 -*-
"""
NAS 宿主机下载（本机 collect + NAS 用 Python urllib 拉 surrit CDN）

说明
----
- 采集：本机 Chrome 扩展 + python download_missav.py --collect
- 下载：本脚本在 NAS 宿主机跑；默认 HTTP 后端为 python（urllib）
- 原因：部分 NAS 系统 curl/OpenSSL 访问 surrit 常被 Cloudflare 403，同机 urllib 可过
- 成片入库：.env 的 NAS_JAV_LIBRARY_DIR（必填）

终端执行指南
------------
注意：SSH 前台直接跑 python3 … 时，断开 SSH 会杀掉进程。
批量下载请用下面的 nohup（或 tmux/screen），不要前台挂着跑。

进度页默认监听 0.0.0.0:8777（局域网）:
  http://<NAS_IP>:8777
关闭进度页加 --no-web；仅本机监听可加 --web-host 127.0.0.1

1. 依赖（NAS 宿主机）:
   sudo -H python3 -m pip install --break-system-packages -r requirements.txt
   # 需已有 ffmpeg（合并 mp4）

2. .env 必填（见 .env.example）:
   NAS_DOWNLOAD_DIR=/path/to/nas_tmp
   NAS_JAV_LIBRARY_DIR=/path/to/library

3. 清单下载（推荐：后台持续执行，断 SSH 不停）:
   ssh user@<NAS_IP>
   cd /path/to/download_from_missav
   mkdir -p logs
   nohup python3 -u download_missav_nas.py --download-only \\
     >> logs/missav_nas_$(date +%Y%m%d_%H%M%S).log 2>&1 &
   echo "started pid $!"
   # 进度页: http://<NAS_IP>:8777
   # 日志:   tail -f logs/missav_nas_*.log
   # 进程:   ps -ef | grep download_missav_nas | grep -v grep
   # 中断后续跑同一命令即可（downloading 分片可续）

   批次结束后自动回查：失败任务以低并发（parallel=1, workers=5）重试
   短间隔 5 轮（60s）+ 长间隔 5 轮（600s），仍失败写入
   logs/download_failed_*.log 并保留临时分片供下次续传

   本机 --collect 更新 check_list2.json 后必须重启本脚本才会吃到新条目
   （启动时读一次清单，运行中不重扫；项目目录与本机共享，无需拷文件）:
   pgrep -af download_missav_nas || echo "未在跑"
   pkill -f download_missav_nas.py || true
   sleep 1
   pgrep -af download_missav_nas && echo "仍未退出，勿重复启动" && exit 1
   nohup python3 -u download_missav_nas.py --download-only \\
     >> logs/missav_nas_$(date +%Y%m%d_%H%M%S).log 2>&1 &
   echo "started pid $!"

4. 前台短测（仅调试；勿用于长任务）:
   python3 download_missav_nas.py --download-only --max-segments 5
   # 或单条:
   # python3 download_missav_nas.py --url "https://missav.ws/dm45/cn/ssni-126" \\
   #   --uuid <uuid> --title "test" --max-segments 5 --no-web

5. 仅同步片库名单:
   python3 download_missav_nas.py --sync-downloaded

6. 强制系统 curl（NAS 上通常 403，仅对照用）:
   python3 download_missav_nas.py --http-backend curl --url ... --uuid ... --max-segments 5 --no-web
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from download_func import (
    DownloadError,
    _env_lookup,
    bootstrap_workspace,
    get_http_backend,
    set_http_backend,
)
from download_missav import run_download_only, run_single_url
from download_progress_web import DEFAULT_WEB_PORT
from missav_tab_check import (
    CHECK_LIST2_JSON,
    DOWNLOADED_JAV_FILE,
    TabCheckError,
    append_downloaded_jav,
    extract_code_from_url,
    load_check_list2,
    move_mp4_to_jav_library,
    sync_downloaded_jav_from_dir,
)


def resolve_nas_download_dir() -> Path:
    """NAS 临时下载目录：NAS_DOWNLOAD_DIR 必填，无默认。"""
    val = _env_lookup("NAS_DOWNLOAD_DIR")
    if not val:
        raise DownloadError(
            "未配置 NAS_DOWNLOAD_DIR。\n"
            "请在环境变量或项目根 .env 中设置，例如:\n"
            "  NAS_DOWNLOAD_DIR=/path/to/nas_tmp"
        )
    return Path(val)


def resolve_nas_jav_library_dir() -> Path:
    """NAS 片库：NAS_JAV_LIBRARY_DIR 必填，无默认。"""
    val = _env_lookup("NAS_JAV_LIBRARY_DIR")
    if not val:
        raise DownloadError(
            "未配置 NAS_JAV_LIBRARY_DIR。\n"
            "请在环境变量或项目根 .env 中设置，例如:\n"
            "  NAS_JAV_LIBRARY_DIR=/path/to/library"
        )
    return Path(val)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MissAV NAS 宿主机下载（默认 Python urllib 拉 CDN；见文件头）"
    )
    parser.add_argument("--url", default=None, help="单条 URL（须配合 --uuid）")
    parser.add_argument("--uuid", default=None, help="surrit UUID")
    parser.add_argument("--title", default=None, help="标题")
    parser.add_argument(
        "--output",
        default=None,
        help="临时目录（默认 NAS_DOWNLOAD_DIR）",
    )
    parser.add_argument("--max-segments", type=int, default=None)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--check-list", default=str(CHECK_LIST2_JSON))
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--keep-segments", action="store_true")
    parser.add_argument("--no-web", action="store_true", help="关闭进度页")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument(
        "--web-host",
        default="0.0.0.0",
        help="进度页监听地址（NAS 默认 0.0.0.0，局域网可访问）",
    )
    parser.add_argument(
        "--sync-downloaded",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="扫描片库写入 downloaded_jav.txt（默认 NAS_JAV_LIBRARY_DIR）",
    )
    parser.add_argument(
        "--http-backend",
        choices=("python", "curl"),
        default="python",
        help="CDN 下载后端（默认 python；curl 在 NAS 上常被 CF 拦）",
    )
    args = parser.parse_args()

    for msg in bootstrap_workspace():
        print(msg)

    max_segments = None if args.full or args.max_segments is None else args.max_segments

    try:
        set_http_backend(args.http_backend)
        jav_library_dir = resolve_nas_jav_library_dir()

        if args.sync_downloaded is not None:
            sync_dir = (
                Path(args.sync_downloaded)
                if args.sync_downloaded
                else jav_library_dir
            )
            codes_n, videos_n, no_code_n = sync_downloaded_jav_from_dir(
                sync_dir,
                DOWNLOADED_JAV_FILE,
            )
            print(
                f"已同步 {DOWNLOADED_JAV_FILE}: {codes_n} 个番号"
                f"（视频 {videos_n}，未能抽番号 {no_code_n}）"
            )
            return

        output_dir = Path(args.output) if args.output else resolve_nas_download_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

        if args.download_only:
            print(f"HTTP 后端: {get_http_backend()}")
            entries = load_check_list2(json_path=Path(args.check_list))
            if not entries:
                raise DownloadError(f"checklist2 为空: {args.check_list}")
            run_download_only(
                entries,
                output_dir,
                max_segments,
                parallel=args.parallel,
                workers=args.workers,
                keep_segments=args.keep_segments,
                enable_web=not args.no_web,
                web_port=args.web_port,
                web_host=args.web_host,
                jav_library_dir=jav_library_dir,
            )
            return

        if not args.url or not args.uuid:
            raise DownloadError("请指定 --download-only，或同时提供 --url 与 --uuid")

        print(f"HTTP 后端: {get_http_backend()}")
        result = run_single_url(
            args.url,
            output_dir,
            max_segments,
            args.workers,
            keep_segments=args.keep_segments,
            video_uuid=args.uuid,
            title=args.title,
        )
        if result.mp4_path and max_segments is None:
            code = extract_code_from_url(args.url)
            if not code:
                raise DownloadError(f"无法从 URL 抽取番号，未入库: {args.url}")
            shelved = move_mp4_to_jav_library(
                result.mp4_path,
                code,
                jav_library_dir,
            )
            append_downloaded_jav(code)
            print(f"已入库: {shelved}")
    except (DownloadError, TabCheckError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
