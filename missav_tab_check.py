# -*- coding: utf-8 -*-
"""
Chrome Tab 检查模块（扩展自动上报 URL + UUID）

扩展安装:
  chrome://extensions → 开发者模式 → 加载 chrome_extension 文件夹

主流程:
  1. 采集（扩展监听 surrit m3u8，点播放即自动上报）:
     python download_missav.py --collect
     在 Chrome 逐个点播放；终端按 Enter 结束采集

  2. 下载（独立，有空再跑）:
     python download_missav.py --download-only
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^FC2-PPV-\d{6,8}", re.I),
    re.compile(r"^FC2PPV\d{6,8}", re.I),
    re.compile(r"^ADV-[A-Z]{0,2}\d{3,5}", re.I),
    re.compile(r"^\d{6}-\d{3}"),
    re.compile(r"^HEYZO-\d{4}", re.I),
    re.compile(r"^[A-Z]{2,10}-\d{2,5}(?:-\d+)?", re.I),
    re.compile(r"^[A-Z]{2,10}\d{2,5}", re.I),
]
URL_SUFFIX_RE = re.compile(
    r"-(?:uncensored-leak|chinese-subtitle|english-subtitle|reducing-mosaic|"
    r"unedited|leak|subtitle)(?:-.*)?$",
    re.I,
)


@dataclass
class List1Entry:
    page_url: str
    code: str | None = None


def _extract_code(name: str) -> str | None:
    stem = Path(name).stem
    if stem.lower().endswith(".m3u8"):
        stem = stem[: -len(".m3u8")]
    for pattern in CODE_PATTERNS:
        match = pattern.match(stem)
        if match:
            return match.group(0).upper()
    return None


def extract_code_from_url(url: str) -> str | None:
    """从 MissAV 页面 URL 的 slug 提取番号。"""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    slug = path.split("/")[-1]
    slug = URL_SUFFIX_RE.sub("", slug)
    slug = slug.replace("_", "-")
    return _extract_code(slug.upper()) or _extract_code(slug.replace("-", " ").upper())

EXTENSION_PORT = 8766
EXTENSION_DIR = Path(__file__).resolve().parent / "chrome_extension"
CHECK_LIST2_FILE = Path(__file__).resolve().parent / "check_list2.txt"
CHECK_LIST2_JSON = Path(__file__).resolve().parent / "check_list2.json"
CHECK_LIST1_FILE = Path(__file__).resolve().parent / "check_list1.txt"
CHECK_LIST1_JSON = Path(__file__).resolve().parent / "check_list1.json"

# 兼容旧文件名
CHECK_LIST_FILE = CHECK_LIST2_FILE
CHECK_LIST_JSON = CHECK_LIST2_JSON
_LEGACY_CHECK_LIST_FILE = Path(__file__).resolve().parent / "check_list.txt"
_LEGACY_CHECK_LIST_JSON = Path(__file__).resolve().parent / "check_list.json"

CHECKLIST2_STATUSES = frozenset(
    {"pending", "ready", "downloading", "download_done", "failed", "downloaded"}
)
# 采集时勿覆盖，避免把进行中的任务重置回 ready
CHECKLIST2_PROGRESS_STATUSES = frozenset(
    {"downloaded", "downloading", "download_done"}
)

MISSAV_LANG = r"cn|en|ja|ko|ms|th|de|fr|vi|id|fil|pt"
MISSAV_EXCLUDE_SLUGS = (
    r"actresses(?:/|$)|playlists(?:/|$)|genres(?:/|$)|makers(?:/|$)|"
    r"tags(?:/|$)|search(?:/|$)|new(?:/|$)|release(?:/|$)|vip(?:/|$)|"
    r"history(?:/|$)|contact(?:/|$)|terms(?:/|$)"
)
MISSAV_VIDEO_URL_RE = re.compile(
    rf"^https?://(?:[\w-]+\.)*missav\.ws/"
    rf"(?:[a-z0-9]+/)?"
    rf"(?:{MISSAV_LANG})/"
    rf"(?!{MISSAV_EXCLUDE_SLUGS})"
    rf"[^\s?#]+/?$",
    re.I,
)
UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.I,
)


class TabCheckError(Exception):
    """Tab 检查失败。"""


@dataclass
class TabItem:
    page_url: str
    title: str
    video_uuid: str
    uuids: list[str] | None = None
    m3u8_url: str | None = None
    quality: str | None = None

    @property
    def uuid_candidates(self) -> list[str]:
        if self.uuids:
            seen: set[str] = set()
            out: list[str] = []
            for u in [self.video_uuid, *self.uuids]:
                if u and UUID_RE.match(u) and u not in seen:
                    seen.add(u)
                    out.append(u)
            return out
        return [self.video_uuid] if self.video_uuid else []

    @classmethod
    def from_dict(cls, data: dict) -> TabItem | None:
        page_url = str(data.get("page_url", "")).strip()
        title = str(data.get("title", "")).strip() or page_url
        raw_uuids = data.get("uuids") or []
        uuids = [
            str(u).strip()
            for u in raw_uuids
            if isinstance(u, str) and UUID_RE.match(str(u).strip())
        ]
        video_uuid = str(data.get("video_uuid", "")).strip() or (uuids[0] if uuids else "")
        if not page_url or not video_uuid or not UUID_RE.match(video_uuid):
            return None
        if not MISSAV_VIDEO_URL_RE.match(page_url):
            return None
        if not uuids:
            uuids = [video_uuid]
        m3u8_url = str(data.get("m3u8_url", "")).strip() or None
        quality = str(data.get("quality", "")).strip() or None
        return cls(
            page_url=page_url,
            title=title,
            video_uuid=video_uuid,
            uuids=uuids,
            m3u8_url=m3u8_url,
            quality=quality,
        )


@dataclass
class List1CheckResult:
    items: list[List1Entry]
    skipped: list[tuple[str, str]]
    total_received: int


@dataclass
class TabCheckResult:
    items: list[TabItem]
    video_urls: list[str]
    all_missav_urls: list[str]
    total_tabs: int


@dataclass
class CheckList2Entry:
    page_url: str
    code: str | None = None
    title: str = ""
    video_uuid: str = ""
    uuids: list[str] | None = None
    m3u8_url: str | None = None
    quality: str | None = None
    status: str = "pending"

    def to_tab_item(self) -> TabItem:
        return TabItem(
            page_url=self.page_url,
            title=self.title or self.page_url,
            video_uuid=self.video_uuid,
            uuids=self.uuids,
            m3u8_url=self.m3u8_url,
            quality=self.quality,
        )

    @classmethod
    def from_dict(cls, data: dict) -> CheckList2Entry | None:
        page_url = str(data.get("page_url", "")).strip()
        if not page_url:
            return None
        raw_uuids = data.get("uuids") or []
        uuids = [
            str(u).strip()
            for u in raw_uuids
            if isinstance(u, str) and UUID_RE.match(str(u).strip())
        ]
        video_uuid = str(data.get("video_uuid", "")).strip() or (uuids[0] if uuids else "")
        status = str(data.get("status", "pending")).strip().lower()
        if status not in CHECKLIST2_STATUSES:
            status = "pending"
        code = data.get("code")
        return cls(
            page_url=page_url,
            code=str(code).upper() if code else extract_code_from_url(page_url),
            title=str(data.get("title", "")).strip() or page_url,
            video_uuid=video_uuid,
            uuids=uuids or None,
            m3u8_url=str(data.get("m3u8_url", "")).strip() or None,
            quality=str(data.get("quality", "")).strip() or None,
            status=status,
        )


@dataclass
class List2CheckResult:
    items: list[CheckList2Entry]
    skipped: list[str]
    failed_probe: list[str]
    failed_parse: list[str] | None = None


def verify_extension_files() -> None:
    manifest = EXTENSION_DIR / "manifest.json"
    if not manifest.is_file():
        raise TabCheckError(
            f"未找到 MissAV 扩展文件: {manifest}\n"
            f"请先在 chrome://extensions 加载目录: {EXTENSION_DIR}"
        )


def filter_missav_urls(urls: list[str]) -> list[str]:
    return [url for url in urls if "missav.ws" in url]


def filter_video_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        normalized = url.rstrip("/")
        if MISSAV_VIDEO_URL_RE.match(url) and normalized not in seen:
            seen.add(normalized)
            result.append(url)
    return result


def _parse_post_body(body: dict) -> list[TabItem]:
    raw_items = body.get("items")
    if isinstance(raw_items, list) and raw_items:
        items: list[TabItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = TabItem.from_dict(raw)
            if item and item.page_url.rstrip("/") not in seen:
                seen.add(item.page_url.rstrip("/"))
                items.append(item)
        return items

    raw_urls = body.get("urls", [])
    if isinstance(raw_urls, list):
        return [
            TabItem(page_url=url, title=url, video_uuid="", uuids=[])
            for url in filter_video_urls(filter_missav_urls([str(u) for u in raw_urls]))
        ]
    return []


def write_check_list1(
    items: list[List1Entry],
    txt_file: Path | str = CHECK_LIST1_FILE,
    json_file: Path | str = CHECK_LIST1_JSON,
) -> None:
    out_txt = Path(txt_file)
    out_json = Path(json_file)
    out_txt.write_text(
        "\n".join(item.page_url for item in items) + ("\n" if items else ""),
        encoding="utf-8",
    )
    out_json.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_check_list1(
    json_path: Path | str = CHECK_LIST1_JSON,
    txt_path: Path | str = CHECK_LIST1_FILE,
) -> list[List1Entry]:
    json_file = Path(json_path)
    if json_file.is_file():
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items: list[List1Entry] = []
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    url = str(row.get("page_url", "")).strip()
                    if url:
                        code = row.get("code")
                        items.append(
                            List1Entry(
                                page_url=url,
                                code=str(code).upper() if code else None,
                            )
                        )
                if items:
                    return items
        except json.JSONDecodeError:
            pass

    return [
        List1Entry(page_url=url, code=None)
        for url in load_check_list(txt_path)
    ]


def _resolve_check_list2_paths(
    json_path: Path | str | None = None,
    txt_path: Path | str | None = None,
) -> tuple[Path, Path]:
    json_file = Path(json_path) if json_path else CHECK_LIST2_JSON
    txt_file = Path(txt_path) if txt_path else CHECK_LIST2_FILE
    return json_file, txt_file


def write_check_list2(
    items: list[CheckList2Entry],
    txt_file: Path | str = CHECK_LIST2_FILE,
    json_file: Path | str = CHECK_LIST2_JSON,
) -> None:
    out_txt = Path(txt_file)
    out_json = Path(json_file)
    txt_body = "\n".join(item.page_url for item in items) + ("\n" if items else "")
    json_body = (
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2) + "\n"
    )
    _atomic_write_text(out_txt, txt_body)
    _atomic_write_text(out_json, json_body)


def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再 replace，避免中断导致半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_check_list2(
    json_path: Path | str | None = None,
    txt_path: Path | str | None = None,
) -> list[CheckList2Entry]:
    candidates = []
    if json_path:
        candidates.append(Path(json_path))
    else:
        candidates.extend([CHECK_LIST2_JSON, _LEGACY_CHECK_LIST_JSON])

    for json_file in candidates:
        if not json_file.is_file():
            continue
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = [CheckList2Entry.from_dict(x) for x in raw if isinstance(x, dict)]
                parsed = [i for i in items if i is not None]
                if parsed:
                    return parsed
        except json.JSONDecodeError:
            continue

    txt_candidates = [Path(txt_path)] if txt_path else [CHECK_LIST2_FILE, _LEGACY_CHECK_LIST_FILE]
    for txt_file in txt_candidates:
        urls = load_check_list(txt_file)
        if urls:
            return [
                CheckList2Entry(page_url=url, code=extract_code_from_url(url), status="pending")
                for url in urls
            ]
    return []


def save_check_list2(items: list[CheckList2Entry]) -> None:
    write_check_list2(items)


def update_check_list2_status(
    page_url: str,
    status: str,
    *,
    json_path: Path | str = CHECK_LIST2_JSON,
    m3u8_url: str | None = None,
    quality: str | None = None,
) -> None:
    items = load_check_list2(json_path=json_path)
    normalized = page_url.rstrip("/")
    for item in items:
        if item.page_url.rstrip("/") == normalized:
            item.status = status
            if m3u8_url:
                item.m3u8_url = m3u8_url
            if quality:
                item.quality = quality
            break
    write_check_list2(items, json_file=json_path)


def write_check_items(items: list[TabItem], txt_file: Path | str = CHECK_LIST_FILE) -> None:
    out_txt = Path(txt_file)
    out_txt.write_text(
        "\n".join(item.page_url for item in items) + ("\n" if items else ""),
        encoding="utf-8",
    )
    write_check_list2(
        [
            CheckList2Entry(
                page_url=item.page_url,
                code=extract_code_from_url(item.page_url),
                title=item.title,
                video_uuid=item.video_uuid,
                uuids=item.uuids,
                m3u8_url=item.m3u8_url,
                quality=item.quality,
                status="ready" if item.video_uuid else "pending",
            )
            for item in items
        ],
        txt_file=out_txt,
    )


def load_check_items(
    json_path: Path | str = CHECK_LIST_JSON,
    txt_path: Path | str = CHECK_LIST_FILE,
) -> list[TabItem]:
    entries = load_check_list2(json_path, txt_path)
    if entries:
        return [entry.to_tab_item() for entry in entries]
    return [
        TabItem(page_url=url, title=url, video_uuid="", uuids=[])
        for url in load_check_list(txt_path)
    ]


def _merge_extension_items(
    tab_items: list[TabItem],
    *,
    by_url: dict[str, CheckList2Entry],
    output_path: Path,
) -> tuple[int, list[str]]:
    """把扩展上报条目合并进 by_url 并落盘；返回 (新增/更新数, 跳过 URL 列表)。"""
    skipped: list[str] = []
    added_or_updated = 0

    for item in tab_items:
        if not item.video_uuid:
            continue
        key = item.page_url.rstrip("/")
        code = extract_code_from_url(item.page_url)
        prev = by_url.get(key)

        if prev and prev.status in CHECKLIST2_PROGRESS_STATUSES:
            skipped.append(item.page_url)
            print(
                f"  跳过进行中/已完成 [{prev.status}] "
                f"[{prev.code or code or '?'}]: {item.page_url}"
            )
            continue
        # 同 URL 已有更高清晰度则保留
        if (
            prev
            and prev.status == "ready"
            and prev.video_uuid == item.video_uuid
            and prev.m3u8_url
            and item.m3u8_url
            and prev.quality
            and item.quality
        ):
            try:
                prev_q = int(str(prev.quality).rstrip("pP"))
                new_q = int(str(item.quality).rstrip("pP"))
                if prev_q >= new_q:
                    continue
            except ValueError:
                pass

        entry = CheckList2Entry(
            page_url=item.page_url,
            code=code or (prev.code if prev else None),
            title=item.title,
            video_uuid=item.video_uuid,
            uuids=item.uuids,
            m3u8_url=item.m3u8_url,
            quality=item.quality,
            status="ready",
        )
        by_url[key] = entry
        added_or_updated += 1
        m3u8_hint = f"  m3u8={item.quality}" if item.m3u8_url else "  (下载时探测 m3u8)"
        print(f"  OK [{entry.code or '?'}] {item.video_uuid}{m3u8_hint}", flush=True)

    if added_or_updated:
        kept_items = list(by_url.values())
        write_check_list2(kept_items, json_file=output_path)
        list1_entries = [
            List1Entry(page_url=e.page_url, code=e.code)
            for e in kept_items
            if e.status != "downloaded"
        ]
        write_check_list1(list1_entries)

    return added_or_updated, skipped


def collect_from_extension(
    output_json: Path | str = CHECK_LIST2_JSON,
    *,
    port: int = EXTENSION_PORT,
    timeout: float = 0,
) -> List2CheckResult:
    """
    主采集：持续接收扩展自动上报（播放即 POST），写入 checklist2。
    默认一直运行，直到终端按 Enter（或 Ctrl+C）。
    timeout>0 时：连续 timeout 秒无新上报则自动结束。
    """
    verify_extension_files()
    output_path = Path(output_json)

    existing = load_check_list2(json_path=output_path)
    by_url: dict[str, CheckList2Entry] = {
        entry.page_url.rstrip("/"): entry for entry in existing
    }

    state_lock = threading.Lock()
    stop_event = threading.Event()
    last_activity = threading.Event()
    last_activity.set()
    total_updated = 0
    all_skipped: list[str] = []
    session_ready = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal total_updated, session_ready
            if self.path not in ("/export", "/tabs"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self.send_error(400)
                return
            items = [i for i in _parse_post_body(body) if i.video_uuid]
            with state_lock:
                updated, skipped = _merge_extension_items(
                    items,
                    by_url=by_url,
                    output_path=output_path,
                )
                total_updated += updated
                all_skipped.extend(skipped)
                session_ready = sum(
                    1 for e in by_url.values() if e.status == "ready"
                )
            last_activity.set()
            payload = json.dumps(
                {
                    "ok": True,
                    "count": updated,
                    "skipped": len(skipped),
                    "ready": session_ready,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise TabCheckError(
            f"无法启动扩展接收服务 127.0.0.1:{port}，端口可能被占用: {exc}"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"本地服务: http://127.0.0.1:{port}/export")
    print(f"扩展目录: {EXTENSION_DIR}")
    print("采集中：在 Chrome 打开视频 Tab 并逐个点击播放（扩展自动上报）")
    print("完成后在本终端按 Enter 结束采集（Ctrl+C 也可）")
    if timeout > 0:
        print(f"若连续 {int(timeout)} 秒无新上报将自动结束")

    def _idle_watcher() -> None:
        if timeout <= 0:
            return
        while not stop_event.is_set():
            if last_activity.wait(timeout):
                last_activity.clear()
                continue
            if not stop_event.is_set():
                print(f"\n已连续 {int(timeout)} 秒无新上报，自动结束采集", flush=True)
                stop_event.set()
            break

    idle_thread = threading.Thread(target=_idle_watcher, daemon=True)
    idle_thread.start()

    try:
        while not stop_event.is_set():
            try:
                input()
                stop_event.set()
                break
            except EOFError:
                stop_event.wait(1.0)
    except KeyboardInterrupt:
        print("\n收到中断，结束采集…", flush=True)
        stop_event.set()

    server.shutdown()
    kept_items = list(by_url.values())
    ready = sum(1 for e in kept_items if e.status == "ready")
    print(
        f"\n清单共 {len(kept_items)} 条 → {output_path}"
        f"（本次新增/更新 {total_updated}，ready={ready}）"
    )
    if all_skipped:
        print(f"去重跳过 {len(all_skipped)} 条")
    if total_updated == 0 and session_ready == 0:
        print(
            "提示: 本次未收到上报。请确认已在 chrome://extensions 重新加载扩展，"
            "并在视频页点击播放。",
            flush=True,
        )

    return List2CheckResult(
        items=kept_items,
        skipped=all_skipped,
        failed_probe=[],
        failed_parse=[],
    )

def load_check_list(path: Path | str = CHECK_LIST_FILE) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    return [
        line.strip()
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
