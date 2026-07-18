# -*- coding: utf-8 -*-
"""
MissAV 下载核心模块（供外部调用）

终端执行指南
------------
Cookie 默认自动加载同目录 missav.ws_cookies.txt（浏览器导出）。
下载目录：项目根目录 .env 中 DOWNLOAD_DIR=...（未配置则默认 D:\downloads）。
单 URL 全自动下载为 mp4（默认下载全量）:
    python download_missav.py --url "https://missav.ws/en/gmem-152-uncensored-leak"

快速测试前 5 分片:
    python download_missav.py --url "https://missav.ws/en/gmem-152-uncensored-leak" --max-segments 5

示例:
    from download_func import download_url

    result = download_url("https://missav.ws/en/gmem-152-uncensored-leak")
    print(result.mp4_path, result.quality)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urljoin, urlparse

_ENV_FILE = Path(__file__).resolve().parent / ".env"
DEFAULT_DOWNLOAD_DIR = Path(r"D:\downloads")


def _read_env_file() -> dict[str, str]:
    if not _ENV_FILE.is_file():
        return {}
    data: dict[str, str] = {}
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def resolve_download_dir() -> Path:
    """下载目录：环境变量 DOWNLOAD_DIR > .env > D:\\downloads。"""
    env_val = os.environ.get("DOWNLOAD_DIR", "").strip()
    if env_val:
        return Path(env_val)
    file_val = _read_env_file().get("DOWNLOAD_DIR", "").strip()
    if file_val:
        return Path(file_val)
    return DEFAULT_DOWNLOAD_DIR


DOWNLOAD_DIR = resolve_download_dir()
TEST_MAX_SEGMENTS = 5
DEFAULT_COOKIE_FILE = Path(__file__).resolve().parent / "missav.ws_cookies.txt"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 无扩展 m3u8 时的探测兜底（含 720p 与 1280x720 两种路径）
QUALITIES = ("1080p", "1920x1080", "720p", "1280x720", "480p", "854x480")
UUID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
)
SURRIT_UUID_RE = re.compile(r"surrit\.com/([a-f0-9-]{36})/")

ProgressCallback = Callable[[int, int], None]
PhaseCallback = Callable[[str], None]

# Windows 上使用 curl.exe（本项目仅在 PC 端运行）
CURL_BIN = "curl.exe" if sys.platform == "win32" else "curl"
SEGMENT_MAX_TIME_S = 120
SEGMENT_MAX_RETRIES = 3
PROBE_MAX_TIME_S = 8
DEFAULT_SEGMENT_WORKERS = 4

# None = 不使用；False = 自动（默认文件存在则加载）；Path = 指定文件
_cookie_file: Path | None | Literal[False] = False


def set_cookie_file(path: Path | str | None | Literal[False] = False) -> None:
    """False=自动默认文件；None=禁用；Path=指定 cookies.txt。"""
    global _cookie_file
    if path is False:
        _cookie_file = False
    elif path is None:
        _cookie_file = None
    else:
        _cookie_file = Path(path)


def resolve_cookie_file() -> Path | None:
    if _cookie_file is None:
        return None
    path = DEFAULT_COOKIE_FILE if _cookie_file is False else _cookie_file
    return path if path.is_file() else None


def curl_cookie_args() -> list[str]:
    cookie = resolve_cookie_file()
    if cookie:
        return ["-b", str(cookie)]
    return []


def _ensure_curl() -> str:
    if shutil.which(CURL_BIN):
        return CURL_BIN
    raise DownloadError(f"未找到 {CURL_BIN}，请安装 curl 或将其加入 PATH")


@dataclass
class DownloadResult:
    page_url: str
    title: str
    video_uuid: str
    quality: str
    m3u8_url: str
    task_dir: Path
    total_segments: int
    downloaded_segments: int
    mp4_path: Path | None = None


class DownloadError(Exception):
    """下载流程中的业务错误。"""


def sanitize_title(title: str, max_len: int = 200) -> str:
    for ch in '\\/:*?"<>|':
        title = title.replace(ch, " ")
    title = " ".join(title.split()).strip()
    if len(title) > max_len:
        title = title[:max_len].rstrip()
    return title


def segments_task_dir(output_root: Path | str, title: str) -> Path:
    """分片任务目录：{output}/{safe_title}.m3u8"""
    return Path(output_root) / f"{sanitize_title(title)}.m3u8"

def referer_origin(page_url: str) -> str:
    parsed = urlparse(page_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def curl_base_args(*, max_time: int = 30, compressed: bool = True) -> list[str]:
    args = [_ensure_curl(), "-sS", "-L", "--http1.1", "--max-time", str(max_time)]
    if compressed:
        args.append("--compressed")
    return args


def curl_segment_args() -> list[str]:
    """分片体积较大，延长超时且不做压缩。"""
    return curl_base_args(max_time=SEGMENT_MAX_TIME_S, compressed=False)


def curl_page_headers(referer: str) -> list[str]:
    return [
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        f"Referer: {referer}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-H",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-H",
        "Sec-Fetch-Dest: document",
        "-H",
        "Sec-Fetch-Mode: navigate",
        "-H",
        "Sec-Fetch-Site: same-origin",
    ]


def curl_headers(referer: str) -> list[str]:
    return [
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        f"Referer: {referer}",
    ]


def _decode_curl_stdout(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _is_cloudflare_block(html: str) -> bool:
    lower = html.lower()
    return (
        "just a moment" in lower
        or "cf-browser-verification" in lower
        or ("cloudflare" in lower and "challenge" in lower)
    )


def curl_fetch_html(url: str, referer: str) -> str:
    cmd = [*curl_base_args(), *curl_cookie_args(), *curl_page_headers(referer), url]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise DownloadError(err or f"页面抓取失败: {url}")
    html = _decode_curl_stdout(result.stdout)
    if not html.strip():
        raise DownloadError(f"页面内容为空: {url}")
    if _is_cloudflare_block(html):
        raise DownloadError("Cloudflare 验证页（页面抓取被拦截，请稍后重试）")
    return html


def extract_uuids(html: str) -> list[str]:
    """按优先级返回候选 UUID：surrit 直链 > 页面内其它 UUID（去重保序）。"""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(uuid: str) -> None:
        if uuid not in seen:
            seen.add(uuid)
            ordered.append(uuid)

    for match in SURRIT_UUID_RE.finditer(html):
        add(match.group(1))
    for uuid in UUID_RE.findall(html):
        add(uuid)
    return ordered


def fetch_page(page_url: str) -> tuple[str, str, str]:
    referer = referer_origin(page_url)
    html = curl_fetch_html(page_url, referer)

    title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
    if not title_match:
        raise DownloadError("无法从页面提取 title")
    title = title_match.group(1).strip()

    uuids = extract_uuids(html)
    if not uuids:
        raise DownloadError("无法从页面提取视频 UUID")

    video_uuid = uuids[0]
    return title, video_uuid, html


def curl_fetch(url: str, referer: str) -> bytes:
    cmd = [*curl_base_args(), *curl_cookie_args(), *curl_headers(referer), url]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise DownloadError(result.stderr.decode("utf-8", errors="replace") or "curl 失败")
    if result.stdout[:15].startswith(b"<!DOCTYPE") or result.stdout[:5].startswith(b"<html"):
        raise DownloadError("CDN 返回 HTML（可能被 Cloudflare 拦截）")
    return result.stdout


def _probe_m3u8_one(video_uuid: str, referer: str) -> tuple[str, str]:
    for quality in QUALITIES:
        url = f"https://surrit.com/{video_uuid}/{quality}/video.m3u8"
        if curl_probe(url, referer):
            return url, quality
    raise DownloadError(f"UUID={video_uuid} 无可用 m3u8")


def _order_uuid_candidates(
    video_uuid: str,
    uuids: list[str] | None = None,
) -> list[str]:
    """video_uuid 优先，其余候选去重保序。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for uuid in [video_uuid, *(uuids or [])]:
        if uuid and uuid not in seen:
            seen.add(uuid)
            ordered.append(uuid)
    return ordered


def probe_m3u8_candidates(
    candidates: list[str],
    page_url: str,
    *,
    max_candidates: int | None = None,
    quiet: bool = False,
) -> tuple[str, str, str]:
    """依次尝试候选 UUID，返回 (m3u8_url, quality, 命中的 uuid)。默认遍历全部候选。"""
    referer = referer_origin(page_url)
    seen: set[str] = set()
    ordered: list[str] = []
    for uuid in candidates:
        if uuid and uuid not in seen:
            seen.add(uuid)
            ordered.append(uuid)
    if max_candidates is not None:
        ordered = ordered[:max_candidates]

    errors: list[str] = []
    for i, uuid in enumerate(ordered, 1):
        if not quiet and len(ordered) > 1:
            print(f"  探测 UUID [{i}/{len(ordered)}] {uuid}")
        try:
            url, quality = _probe_m3u8_one(uuid, referer)
            if not quiet and len(ordered) > 1:
                print(f"  → 命中 {quality}  {uuid}")
            return url, quality, uuid
        except DownloadError as exc:
            errors.append(str(exc))
    raise DownloadError(
        f"未找到可用 m3u8，已尝试 {len(ordered)} 个 UUID: {'; '.join(errors[:3])}"
        + (f" …共 {len(errors)} 条" if len(errors) > 3 else "")
    )


def resolve_last_working_uuid(
    candidates: list[str],
    page_url: str,
    *,
    quiet: bool = True,
) -> tuple[str, str, str] | None:
    """优先校验候选列表末尾 UUID；命中即返回，否则从尾到头依次回退。"""
    referer = referer_origin(page_url)
    seen: set[str] = set()
    ordered: list[str] = []
    for uuid in candidates:
        if uuid and uuid not in seen:
            seen.add(uuid)
            ordered.append(uuid)
    if not ordered:
        return None

    try_order = list(reversed(ordered))
    for i, uuid in enumerate(try_order, 1):
        if not quiet and len(ordered) > 1:
            label = "末尾 UUID" if i == 1 else f"回退 [{i}/{len(try_order)}]"
            print(f"  校验 {label}: {uuid}")
        try:
            url, quality = _probe_m3u8_one(uuid, referer)
            if not quiet:
                if i == 1:
                    print(f"  → 命中 {quality}  {uuid}")
                else:
                    print(f"  → 回退命中 {quality}  {uuid}")
            return url, quality, uuid
        except DownloadError:
            continue
    return None


def probe_m3u8(video_uuid: str, page_url: str, html: str | None = None) -> tuple[str, str]:
    referer = referer_origin(page_url)
    candidates = [video_uuid]
    if html:
        for uuid in extract_uuids(html):
            if uuid not in candidates:
                candidates.append(uuid)

    errors: list[str] = []
    for uuid in candidates:
        try:
            return _probe_m3u8_one(uuid, referer)
        except DownloadError as exc:
            errors.append(str(exc))
    raise DownloadError(
        f"未找到可用 m3u8，已尝试 {len(candidates)} 个 UUID: {'; '.join(errors)}"
    )


def curl_probe(url: str, referer: str) -> bool:
    cmd = [
        _ensure_curl(),
        "-sI",
        "--http1.1",
        "--max-time",
        str(PROBE_MAX_TIME_S),
        *curl_cookie_args(),
        *curl_headers(referer),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[0:1]
    return bool(first_line and "200" in first_line[0])


def curl_download(url: str, referer: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return

    last_err = ""
    for attempt in range(1, SEGMENT_MAX_RETRIES + 1):
        if dest.exists():
            dest.unlink()
        cmd = [
            *curl_segment_args(),
            *curl_cookie_args(),
            *curl_headers(referer),
            "-o",
            str(dest),
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, check=False)
        if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return
        last_err = result.stderr.decode("utf-8", errors="replace") or f"下载失败: {url}"
        if dest.exists():
            dest.unlink()

    raise DownloadError(last_err)


def parse_m3u8(content: str) -> list[tuple[float, str]]:
    segments: list[tuple[float, str]] = []
    duration = 0.0
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].rstrip(","))
        elif line and not line.startswith("#"):
            segments.append((duration, line))
    return segments


def build_local_m3u8_from_count(segments: list[tuple[float, str]], downloaded_count: int) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:5",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(downloaded_count):
        duration, _ = segments[i]
        lines.append(f"#EXTINF:{duration},")
        lines.append(f"index/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def download_hls(
    page_url: str,
    m3u8_url: str,
    referer: str,
    output_root: Path,
    title: str,
    max_segments: int | None = None,
    workers: int = 8,
    on_progress: ProgressCallback | None = None,
) -> tuple[Path, int, int]:
    safe_title = sanitize_title(title)
    task_dir = output_root / f"{safe_title}.m3u8"
    index_dir = task_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    playlist_bytes = curl_fetch(m3u8_url, referer)
    segments = parse_m3u8(playlist_bytes.decode("utf-8", errors="replace"))
    if not segments:
        raise DownloadError("m3u8 播放列表为空")

    total = len(segments) if max_segments is None else min(max_segments, len(segments))
    m3u8_base = m3u8_url.rsplit("/", 1)[0] + "/"

    if on_progress:
        on_progress(0, total)

    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(total):
            _, seg_name = segments[i]
            dest = index_dir / f"{i}.ts"
            fut = pool.submit(download_segment, m3u8_base, seg_name, referer, dest)
            futures[fut] = i

        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                fut.result()
            except DownloadError as exc:
                raise DownloadError(f"分片 {idx} 下载失败: {exc}") from exc
            done += 1
            if on_progress:
                on_progress(done, total)

    playlist = build_local_m3u8_from_count(segments, total)
    (task_dir / "index.m3u8").write_text(playlist, encoding="utf-8")
    return task_dir, len(segments), total


def download_segment(
    m3u8_base: str,
    segment_name: str,
    referer: str,
    dest: Path,
) -> None:
    url = urljoin(m3u8_base, segment_name)
    curl_download(url, referer, dest)


def find_ffmpeg() -> str:
    if path := shutil.which("ffmpeg"):
        return path
    raise DownloadError("未找到 ffmpeg，请安装并加入 PATH")


def merge_hls_to_mp4(
    task_dir: Path,
    output_root: Path,
    title: str,
    *,
    keep_segments: bool = False,
) -> Path:
    """将 download_hls 产物合并为单个 mp4。"""
    m3u8_file = task_dir / "index.m3u8"
    if not m3u8_file.is_file():
        raise DownloadError(f"未找到本地播放列表: {m3u8_file}")

    safe_title = sanitize_title(title)
    mp4_path = output_root / f"{safe_title}.mp4"
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        find_ffmpeg(),
        "-y",
        "-allowed_extensions",
        "ALL",
        "-fflags",
        "+genpts",
        "-i",
        str(m3u8_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(mp4_path),
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise DownloadError(err or f"ffmpeg 合并失败: {mp4_path}")
    if not mp4_path.is_file() or mp4_path.stat().st_size == 0:
        raise DownloadError(f"合并后 mp4 为空: {mp4_path}")

    if not keep_segments:
        shutil.rmtree(task_dir, ignore_errors=True)

    return mp4_path


def download_from_tab(
    page_url: str,
    video_uuid: str,
    title: str,
    *,
    m3u8_url: str | None = None,
    quality: str | None = None,
    uuids: list[str] | None = None,
    output_dir: Path | str = DOWNLOAD_DIR,
    max_segments: int | None = None,
    workers: int = 8,
    on_progress: ProgressCallback | None = None,
    merge_mp4: bool = True,
    keep_segments: bool = False,
    on_phase: PhaseCallback | None = None,
) -> DownloadResult:
    """从 list2 已校验的 UUID/m3u8 下载，不再探测候选 UUID。"""
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    referer = referer_origin(page_url)
    resolved_uuid = video_uuid
    if m3u8_url and quality:
        pass
    else:
        candidates = _order_uuid_candidates(video_uuid, uuids)
        if on_phase:
            on_phase("probe")
        m3u8_url, quality, resolved_uuid = probe_m3u8_candidates(candidates, page_url)

    if on_phase:
        on_phase("segments")
    task_dir, total_segments, downloaded_segments = download_hls(
        page_url=page_url,
        m3u8_url=m3u8_url,
        referer=referer,
        output_root=output_root,
        title=title,
        max_segments=max_segments,
        workers=workers,
        on_progress=on_progress,
    )

    mp4_path: Path | None = None
    if merge_mp4:
        if on_phase:
            on_phase("merge")
        mp4_path = merge_hls_to_mp4(
            task_dir, output_root, title, keep_segments=keep_segments
        )

    return DownloadResult(
        page_url=page_url,
        title=title,
        video_uuid=resolved_uuid,
        quality=quality,
        m3u8_url=m3u8_url,
        task_dir=task_dir if keep_segments else mp4_path.parent if mp4_path else task_dir,
        total_segments=total_segments,
        downloaded_segments=downloaded_segments,
        mp4_path=mp4_path,
    )


def download_url(
    page_url: str,
    *,
    output_dir: Path | str = DOWNLOAD_DIR,
    max_segments: int | None = None,
    workers: int = 8,
    on_progress: ProgressCallback | None = None,
    merge_mp4: bool = True,
    keep_segments: bool = False,
    video_uuid: str | None = None,
    title: str | None = None,
    on_phase: PhaseCallback | None = None,
) -> DownloadResult:
    """已知 UUID 时下载并合并为 mp4（不再解析 missav 页面）。"""
    if not video_uuid:
        raise DownloadError(
            "download_url 需要 video_uuid；请先用扩展 --collect 采集，或传入 --uuid"
        )
    return download_from_tab(
        page_url,
        video_uuid,
        title or page_url,
        uuids=[video_uuid],
        output_dir=output_dir,
        max_segments=max_segments,
        workers=workers,
        on_progress=on_progress,
        merge_mp4=merge_mp4,
        keep_segments=keep_segments,
        on_phase=on_phase,
    )


def download(
    page_url: str,
    *,
    output_dir: Path | str = DOWNLOAD_DIR,
    max_segments: int | None = None,
    workers: int = 8,
    on_progress: ProgressCallback | None = None,
    merge_mp4: bool = True,
    keep_segments: bool = False,
) -> DownloadResult:
    """
    下载单个 missav 页面视频（curl 抓页，易被拦截；推荐先 --collect 再下载）。

    Args:
        page_url: missav 页面 URL
        output_dir: 下载根目录，默认见 .env 的 DOWNLOAD_DIR 或用户 Downloads
        max_segments: 最多下载分片数，None 表示全部
        workers: 分片并发数
        on_progress: 进度回调 (done, total)
        merge_mp4: 是否合并为 mp4
        keep_segments: 合并后是否保留分片目录

    Returns:
        DownloadResult 包含 mp4 路径、清晰度等信息
    """
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    title, video_uuid, html = fetch_page(page_url)
    referer = referer_origin(page_url)
    m3u8_url, quality = probe_m3u8(video_uuid, page_url, html=html)

    task_dir, total_segments, downloaded_segments = download_hls(
        page_url=page_url,
        m3u8_url=m3u8_url,
        referer=referer,
        output_root=output_root,
        title=title,
        max_segments=max_segments,
        workers=workers,
        on_progress=on_progress,
    )

    mp4_path: Path | None = None
    if merge_mp4:
        mp4_path = merge_hls_to_mp4(
            task_dir, output_root, title, keep_segments=keep_segments
        )

    return DownloadResult(
        page_url=page_url,
        title=title,
        video_uuid=video_uuid,
        quality=quality,
        m3u8_url=m3u8_url,
        task_dir=task_dir if keep_segments else mp4_path.parent if mp4_path else task_dir,
        total_segments=total_segments,
        downloaded_segments=downloaded_segments,
        mp4_path=mp4_path,
    )
