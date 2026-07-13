# -*- coding: utf-8 -*-
"""
终端执行指南
------------
1. 安装依赖（首次）:
   pip install -r requirements.txt

2. 采集（扩展自动上报，无需点导出）:
   python download_missav.py --collect
   → Chrome 打开视频 Tab，逐个点击播放
   → 扩展捕获 surrit m3u8 后自动写入 check_list2.json
   → 终端按 Enter 结束采集

3. 下载（独立，有空再跑）:
   python download_missav.py --download-only
   python download_missav.py --download-only --workers 8
   → 状态: ready → downloading → download_done → downloaded
   → 重启优先: download_done(只合并) > downloading(续下) > ready
   → 默认开启进度页（http://127.0.0.1:8777）；--no-web 可关闭

   快速测试（仅前 5 分片）: 加 --max-segments 5
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from download_func import (
    DOWNLOAD_DIR,
    DownloadError,
    DownloadResult,
    download,
    download_from_tab,
    download_url,
    merge_hls_to_mp4,
    segments_task_dir,
)
from download_progress_web import (
    DEFAULT_WEB_PORT,
    ProgressStore,
    ProgressWebServer,
)
from missav_tab_check import (
    CHECK_LIST2_JSON,
    EXTENSION_DIR,
    CheckList2Entry,
    TabCheckError,
    TabItem,
    collect_from_extension,
    load_check_list2,
    save_check_list2,
)

MAX_PARALLEL_TASKS = 2
SEGMENT_WORKERS = 4

PHASE_LABELS = {
    "parse": "解析页面",
    "probe": "探测 m3u8",
    "segments": "下载分片",
    "merge": "合并 mp4",
}

PROGRESS_COLUMNS = (
    SpinnerColumn(),
    TextColumn("[bold blue]{task.description}"),
    BarColumn(bar_width=32),
    MofNCompleteColumn(),
    TaskProgressColumn(),
    TimeElapsedColumn(),
    TimeRemainingColumn(),
)


def _short_label(text: str, width: int = 42) -> str:
    if len(text) <= width:
        return text
    return text[: width // 2 - 1] + "…" + text[-(width // 2 - 2) :]


def _phase_desc(title: str, phase: str, done: int = 0, total: int = 0) -> str:
    label = PHASE_LABELS.get(phase, phase)
    name = _short_label(title)
    if phase == "segments" and total > 0:
        return f"[{label}] {name}  {done}/{total}"
    return f"[{label}] {name}"


def _download_item(
    item: TabItem,
    output_dir: Path,
    max_segments: int | None,
    workers: int,
    on_progress,
    on_phase=None,
    *,
    keep_segments: bool = False,
    merge_mp4: bool = False,
) -> DownloadResult:
    if item.video_uuid and item.m3u8_url and item.quality:
        return download_from_tab(
            item.page_url,
            item.video_uuid,
            item.title,
            m3u8_url=item.m3u8_url,
            quality=item.quality,
            output_dir=output_dir,
            max_segments=max_segments,
            workers=workers,
            on_progress=on_progress,
            on_phase=on_phase,
            merge_mp4=merge_mp4,
            keep_segments=keep_segments,
        )
    if item.video_uuid:
        return download_from_tab(
            item.page_url,
            item.video_uuid,
            item.title,
            uuids=item.uuid_candidates,
            output_dir=output_dir,
            max_segments=max_segments,
            workers=workers,
            on_progress=on_progress,
            on_phase=on_phase,
            merge_mp4=merge_mp4,
            keep_segments=keep_segments,
        )
    return download(
        item.page_url,
        output_dir=output_dir,
        max_segments=max_segments,
        workers=workers,
        on_progress=on_progress,
        merge_mp4=merge_mp4,
        keep_segments=keep_segments,
    )


def _download_segments_with_progress(
    progress: Progress,
    task_id,
    item: TabItem,
    output_dir: Path,
    max_segments: int | None,
    workers: int,
    progress_lock: threading.Lock,
    store: ProgressStore | None = None,
) -> DownloadResult:
    """只下载分片，不合并；完成后进度切到「合并」排队态。"""
    title = item.title or item.page_url
    key = item.page_url

    def on_phase(phase: str) -> None:
        if store is not None:
            store.set_phase(key, phase)
        with progress_lock:
            if phase == "segments":
                progress.update(
                    task_id,
                    description=_phase_desc(title, phase, 0, 1),
                    completed=0,
                    total=1,
                )
            else:
                progress.update(
                    task_id,
                    description=_phase_desc(title, phase),
                    completed=0,
                    total=1,
                )

    def on_progress(done: int, total: int) -> None:
        if store is not None:
            store.set_progress(key, done, total)
        with progress_lock:
            progress.update(
                task_id,
                description=_phase_desc(title, "segments", done, total),
                completed=done,
                total=total,
            )

    result = _download_item(
        item,
        output_dir,
        max_segments,
        workers,
        on_progress,
        on_phase=on_phase,
        merge_mp4=False,
    )
    if store is not None:
        store.set_phase(key, "merge")
    with progress_lock:
        progress.update(
            task_id,
            description=_phase_desc(result.title, "merge"),
            completed=0,
            total=1,
        )
    return result


def _merge_with_progress(
    progress: Progress,
    task_id,
    result: DownloadResult,
    output_dir: Path,
    progress_lock: threading.Lock,
    *,
    keep_segments: bool = False,
    store: ProgressStore | None = None,
) -> DownloadResult:
    """在独立线程中合并 mp4，不占用下载并行槽。"""
    if store is not None:
        store.set_phase(result.page_url, "merge")
    with progress_lock:
        progress.update(
            task_id,
            description=_phase_desc(result.title, "merge"),
            completed=0,
            total=1,
        )
    mp4_path = merge_hls_to_mp4(
        result.task_dir,
        Path(output_dir),
        result.title,
        keep_segments=keep_segments,
    )
    result.mp4_path = mp4_path
    if not keep_segments:
        result.task_dir = mp4_path.parent
    if store is not None:
        store.mark_done(
            result.page_url,
            done=result.downloaded_segments,
            total=max(result.total_segments, result.downloaded_segments, 1),
        )
    with progress_lock:
        progress.update(
            task_id,
            description=f"[完成] {_short_label(result.title)}",
            completed=result.downloaded_segments,
            total=max(result.total_segments, result.downloaded_segments, 1),
        )
    return result


def _result_from_existing_segments(item: TabItem, output_dir: Path) -> DownloadResult:
    """download_done 续跑：用已有分片目录构造 DownloadResult，只合并。"""
    title = item.title or item.page_url
    task_dir = segments_task_dir(output_dir, title)
    m3u8_file = task_dir / "index.m3u8"
    if not m3u8_file.is_file():
        raise DownloadError(f"download_done 但缺少分片播放列表: {m3u8_file}")
    index_dir = task_dir / "index"
    n = len(list(index_dir.glob("*.ts"))) if index_dir.is_dir() else 0
    return DownloadResult(
        page_url=item.page_url,
        title=title,
        video_uuid=item.video_uuid or "",
        quality=item.quality or "",
        m3u8_url=item.m3u8_url or "",
        task_dir=task_dir,
        total_segments=n,
        downloaded_segments=n,
    )


def _run_download_merge_pipeline(
    download_items: list[TabItem],
    merge_only_items: list[TabItem],
    output_dir: Path,
    max_segments: int | None,
    parallel: int,
    workers: int,
    *,
    keep_segments: bool = False,
    on_download_start=None,
    on_segments_done=None,
    on_success=None,
    on_failure=None,
    store: ProgressStore | None = None,
) -> tuple[list[DownloadResult], list[tuple[str, Exception]]]:
    """
    下载与合并流水线：
    - merge_only_items: 已是 download_done，只合并
    - download_items: downloading/ready，先下分片再合并
    - 分片下完即释放下载槽；合并在独立线程池
    """
    results: list[DownloadResult] = []
    errors: list[tuple[str, Exception]] = []
    progress_lock = threading.Lock()
    all_items = list(merge_only_items) + list(download_items)

    if store is not None:
        store.reset(
            [(item.page_url, item.title or item.page_url) for item in all_items],
            output_dir=str(output_dir),
            parallel=parallel,
            workers=workers,
        )

    with Progress(*PROGRESS_COLUMNS, transient=False) as progress:
        task_ids = {
            item.page_url: progress.add_task(
                f"[排队] {_short_label(item.title or item.page_url)}",
                total=1,
            )
            for item in all_items
        }

        def do_download(item: TabItem) -> DownloadResult:
            if on_download_start:
                on_download_start(item)
            return _download_segments_with_progress(
                progress,
                task_ids[item.page_url],
                item,
                output_dir,
                max_segments,
                workers,
                progress_lock,
                store=store,
            )

        def do_merge(result: DownloadResult) -> DownloadResult:
            return _merge_with_progress(
                progress,
                task_ids[result.page_url],
                result,
                output_dir,
                progress_lock,
                keep_segments=keep_segments,
                store=store,
            )

        def mark_fail(item: TabItem, exc: Exception) -> None:
            errors.append((item.page_url, exc))
            if store is not None:
                store.mark_failed(item.page_url, str(exc))
            if on_failure:
                on_failure(item, exc)
            with progress_lock:
                progress.update(
                    task_ids[item.page_url],
                    description=f"[失败] {_short_label(item.title or item.page_url)}",
                )

        with ThreadPoolExecutor(max_workers=parallel) as download_pool:
            with ThreadPoolExecutor(max_workers=parallel) as merge_pool:
                merge_futs: dict = {}

                # 优先提交「只合并」任务
                for item in merge_only_items:
                    try:
                        dl_result = _result_from_existing_segments(item, output_dir)
                        if store is not None:
                            store.set_progress(
                                item.page_url,
                                dl_result.downloaded_segments,
                                max(dl_result.total_segments, 1),
                            )
                            store.set_phase(item.page_url, "merge")
                        with progress_lock:
                            progress.update(
                                task_ids[item.page_url],
                                description=_phase_desc(
                                    item.title or item.page_url, "merge"
                                ),
                                completed=0,
                                total=1,
                            )
                        merge_futs[merge_pool.submit(do_merge, dl_result)] = item
                    except Exception as exc:
                        mark_fail(item, exc)

                download_futs = {
                    download_pool.submit(do_download, item): item
                    for item in download_items
                }

                for fut in as_completed(download_futs):
                    item = download_futs[fut]
                    try:
                        dl_result = fut.result()
                        if on_segments_done:
                            on_segments_done(item, dl_result)
                        merge_futs[merge_pool.submit(do_merge, dl_result)] = item
                    except Exception as exc:
                        mark_fail(item, exc)

                for fut in as_completed(merge_futs):
                    item = merge_futs[fut]
                    try:
                        result = fut.result()
                        results.append(result)
                        if on_success:
                            on_success(item, result)
                    except Exception as exc:
                        mark_fail(item, exc)

    return results, errors


def run_parallel(
    items: list[TabItem],
    output_dir: Path,
    max_segments: int | None,
    parallel: int,
    workers: int,
    *,
    keep_segments: bool = False,
    enable_web: bool = True,
    web_port: int = DEFAULT_WEB_PORT,
) -> list[DownloadResult]:
    store: ProgressStore | None = None
    web: ProgressWebServer | None = None
    results: list[DownloadResult] = []
    errors: list[tuple[str, Exception]] = []
    if enable_web:
        store = ProgressStore()
        web = ProgressWebServer(store, port=web_port)
        print(f"进度页: {web.start()}")

    try:
        results, errors = _run_download_merge_pipeline(
            download_items=items,
            merge_only_items=[],
            output_dir=output_dir,
            max_segments=max_segments,
            parallel=parallel,
            workers=workers,
            keep_segments=keep_segments,
            store=store,
        )
    finally:
        if web is not None:
            web.stop()

    print(f"\n完成 {len(results)}/{len(items)} 个任务")
    for r in results:
        dest = r.mp4_path or r.task_dir
        print(f"  OK {r.title}")
        print(f"    → {dest}")
    for url, exc in errors:
        print(f"  FAIL {_short_label(url)}: {exc}", file=sys.stderr)

    if errors and not results:
        raise DownloadError(f"全部 {len(errors)} 个任务失败")
    return results


def run_download_only(
    entries: list[CheckList2Entry],
    output_dir: Path,
    max_segments: int | None,
    parallel: int = MAX_PARALLEL_TASKS,
    workers: int = SEGMENT_WORKERS,
    *,
    keep_segments: bool = False,
    enable_web: bool = True,
    web_port: int = DEFAULT_WEB_PORT,
) -> list[DownloadResult]:
    merge_only_entries: list[CheckList2Entry] = []
    download_entries: list[CheckList2Entry] = []
    demoted = 0
    for entry in entries:
        if not entry.video_uuid:
            continue
        if entry.status == "download_done":
            title = entry.title or entry.page_url
            task_dir = segments_task_dir(output_dir, title)
            if (task_dir / "index.m3u8").is_file():
                merge_only_entries.append(entry)
            else:
                # 分片目录丢了，退回重新下载
                entry.status = "ready"
                demoted += 1
                download_entries.append(entry)
        elif entry.status == "downloading":
            download_entries.append(entry)
        elif entry.status == "ready":
            download_entries.append(entry)

    # downloading 优先于 ready
    download_entries.sort(key=lambda e: 0 if e.status == "downloading" else 1)

    if demoted:
        save_check_list2(entries)
        print(f"有 {demoted} 条 download_done 缺少分片，已退回 ready 重新下载")

    if not merge_only_entries and not download_entries:
        raise DownloadError(
            "checklist2 中没有可处理条目"
            "（需要 ready / downloading / download_done 且含 video_uuid）\n"
            "请先运行 --collect，在 Chrome 逐个点播放（扩展自动上报）"
        )

    all_by_url = {e.page_url.rstrip("/"): e for e in entries}
    merge_only_items = [e.to_tab_item() for e in merge_only_entries]
    download_items = [e.to_tab_item() for e in download_entries]
    checklist_lock = threading.Lock()
    total_n = len(merge_only_items) + len(download_items)

    print(
        f"\n开始批量下载 {total_n} 个任务"
        f"（只合并 {len(merge_only_items)}，下载 {len(download_items)}；"
        f"视频并行 {parallel}，每视频分片线程 {workers}"
        f"{'' if max_segments is None else f'，测试限 {max_segments} 分片'}）"
    )

    store: ProgressStore | None = None
    web: ProgressWebServer | None = None
    results: list[DownloadResult] = []
    errors: list[tuple[str, Exception]] = []
    if enable_web:
        store = ProgressStore()
        web = ProgressWebServer(store, port=web_port)
        print(f"进度页: {web.start()}")

    print(f"输出目录: {output_dir}")
    print("优先级: download_done(只合并) > downloading(续下) > ready")

    def persist_checklist() -> None:
        with checklist_lock:
            save_check_list2(entries)

    def on_download_start(item: TabItem) -> None:
        entry = all_by_url.get(item.page_url.rstrip("/"))
        if entry and entry.status != "downloading":
            entry.status = "downloading"
            persist_checklist()

    def on_segments_done(item: TabItem, _result: DownloadResult) -> None:
        entry = all_by_url.get(item.page_url.rstrip("/"))
        if entry:
            entry.status = "download_done"
            persist_checklist()

    def on_success(item: TabItem, _result: DownloadResult) -> None:
        entry = all_by_url.get(item.page_url.rstrip("/"))
        if entry:
            entry.status = "downloaded"
            persist_checklist()

    def on_failure(item: TabItem, exc: Exception) -> None:
        entry = all_by_url.get(item.page_url.rstrip("/"))
        if not entry:
            return
        msg = str(exc)
        if entry.status == "download_done" and "缺少分片" in msg:
            entry.status = "ready"
        elif entry.status == "download_done":
            # 合并失败：保留 download_done，下次只合并
            pass
        else:
            entry.status = "failed"
        persist_checklist()

    try:
        results, errors = _run_download_merge_pipeline(
            download_items=download_items,
            merge_only_items=merge_only_items,
            output_dir=output_dir,
            max_segments=max_segments,
            parallel=parallel,
            workers=workers,
            keep_segments=keep_segments,
            on_download_start=on_download_start,
            on_segments_done=on_segments_done,
            on_success=on_success,
            on_failure=on_failure,
            store=store,
        )

        persist_checklist()

        print(f"\n完成 {len(results)}/{total_n} 个任务")
        for r in results:
            dest = r.mp4_path or r.task_dir
            print(f"  OK {r.title}")
            print(f"    → {dest}")
        for url, exc in errors:
            print(f"  FAIL {_short_label(url)}: {exc}", file=sys.stderr)
    finally:
        if web is not None:
            web.stop()

    if errors and not results:
        raise DownloadError(f"全部 {len(errors)} 个任务失败")
    return results


def run_single_url(
    page_url: str,
    output_dir: Path,
    max_segments: int | None,
    workers: int = SEGMENT_WORKERS,
    *,
    keep_segments: bool = False,
    video_uuid: str | None = None,
    title: str | None = None,
) -> DownloadResult:
    if not video_uuid:
        raise DownloadError(
            "单条下载必须提供 --uuid（已移除 Playwright 页面解析）。\n"
            "请用 --collect 采集，或手动指定 --uuid 与可选 --title"
        )

    progress_lock = threading.Lock()
    title_holder = {"title": title or page_url}

    with Progress(*PROGRESS_COLUMNS, transient=False) as progress:
        task_id = progress.add_task("[探测 m3u8] …", total=1)

        def on_phase(phase: str) -> None:
            with progress_lock:
                t = title_holder["title"]
                if phase == "segments":
                    progress.update(
                        task_id,
                        description=_phase_desc(t, phase, 0, 1),
                        completed=0,
                        total=1,
                    )
                else:
                    progress.update(
                        task_id,
                        description=_phase_desc(t, phase),
                        completed=0,
                        total=1,
                    )

        def on_progress(done: int, total: int) -> None:
            with progress_lock:
                progress.update(
                    task_id,
                    description=_phase_desc(title_holder["title"], "segments", done, total),
                    completed=done,
                    total=total,
                )

        result = download_url(
            page_url,
            output_dir=output_dir,
            max_segments=max_segments,
            workers=workers,
            on_progress=on_progress,
            on_phase=on_phase,
            merge_mp4=True,
            keep_segments=keep_segments,
            video_uuid=video_uuid,
            title=title,
        )
        title_holder["title"] = result.title

        with progress_lock:
            progress.update(
                task_id,
                description=f"[完成] {_short_label(result.title)}",
                completed=result.downloaded_segments,
                total=max(result.total_segments, result.downloaded_segments, 1),
            )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="MissAV 下载入口")
    parser.add_argument("--url", default=None, help="下载单个 missav 页面 URL（须配合 --uuid）")
    parser.add_argument(
        "--uuid",
        default=None,
        help="已知 surrit UUID（单条 --url 下载必填）",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="视频完整标题（与 --uuid 同用）",
    )
    parser.add_argument(
        "--output",
        default=str(DOWNLOAD_DIR),
        help=f"下载根目录（默认 {DOWNLOAD_DIR}）",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=None,
        help="最多下载分片数（默认不限，下载全量；测试时可设为 5）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="下载全部分片（默认行为，可省略）",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="主采集：持续接收扩展自动上报（点播放即可）→ check_list2.json；Enter 结束",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="读 checklist2 批量下载（独立；需先 --collect）",
    )
    parser.add_argument(
        "--check-list",
        default=str(CHECK_LIST2_JSON),
        help="checklist2 JSON 路径",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=SEGMENT_WORKERS,
        help=f"每个视频的分片下载线程数（默认 {SEGMENT_WORKERS}）",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=MAX_PARALLEL_TASKS,
        help=f"同时下载的视频数（默认 {MAX_PARALLEL_TASKS}）",
    )
    parser.add_argument(
        "--keep-segments",
        action="store_true",
        help="合并 mp4 后保留分片目录（默认删除）",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="关闭本地下载进度页（默认开启，地址打印在「开始批量下载」下方）",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"进度页端口（默认 {DEFAULT_WEB_PORT}）",
    )
    args = parser.parse_args()

    check_list_path = Path(args.check_list)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    max_segments = None if args.full or args.max_segments is None else args.max_segments

    try:
        if args.collect:
            print("采集：等待扩展自动上报（点播放即可）…")
            print(f"扩展目录: {EXTENSION_DIR}")
            result = collect_from_extension(
                output_json=check_list_path,
            )
            ready = sum(1 for item in result.items if item.status == "ready")
            print(f"可下载（ready）: {ready} 条。有空后运行: --download-only")
            return

        if args.url:
            result = run_single_url(
                args.url,
                output_dir,
                max_segments,
                args.workers,
                keep_segments=args.keep_segments,
                video_uuid=args.uuid,
                title=args.title,
            )
            dest = result.mp4_path or result.task_dir
            print(f"\n完成: {result.title}")
            print(f"文件: {dest}")
            return

        if args.download_only:
            entries = load_check_list2(json_path=check_list_path)
            if not entries:
                raise DownloadError(
                    f"checklist2 为空: {check_list_path}\n"
                    "请先运行 --collect"
                )
            run_download_only(
                entries,
                output_dir,
                max_segments,
                parallel=args.parallel,
                workers=args.workers,
                keep_segments=args.keep_segments,
                enable_web=not args.no_web,
                web_port=args.web_port,
            )
            return

        # 无参数时等同 --collect
        print("采集：等待扩展自动上报（点播放即可）…")
        print(f"扩展目录: {EXTENSION_DIR}")
        result = collect_from_extension(
            output_json=check_list_path,
        )
        ready = sum(1 for item in result.items if item.status == "ready")
        print(f"可下载（ready）: {ready} 条。有空后运行: --download-only")

    except (DownloadError, TabCheckError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
