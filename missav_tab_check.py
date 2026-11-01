# -*- coding: utf-8 -*-
"""
Chrome Tab 检查模块（扩展自动上报 URL + UUID）

终端执行指南
------------
扩展安装 / 更新后重载:
  chrome://extensions → 开发者模式 → 加载或「重新加载」chrome_extension 文件夹

主流程:
  1. 采集（扩展嗅探 surrit …/{uuid}/{任意清晰度}/video.m3u8，点播放即上报）:
     python download_missav.py --collect
     在 Chrome 逐个点播放；终端按 Enter 结束采集
     封面由扩展下载后 POST /cover，保存到项目 covers/{番号}.jpg
     资料齐全时写入 pending/{番号}/（movie.nfo、poster.jpg、fanart.jpg、metadata.json、job.json）
     缺番号、标题、女优、类型、发行日期、简介、发行商、封面时标为 incomplete，不进入下载

  2. 下载（独立，有空再跑）:
     python download_missav.py --download-only
     只处理 pending 目录已定稿的条目；合并后补时长，整夹搬入片库
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from missav_metadata import (
    MetadataPackageError,
    missing_collect_fields,
    prepare_pending_dir,
    safe_path_component,
)

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


def normalize_code(code: str) -> str:
    """番号归一化：去非字母数字，便于 SSIS-526 / SSIS526 互认。"""
    return re.sub(r"[^A-Z0-9]", "", code.upper())


EXTENSION_PORT = 8766
EXTENSION_DIR = Path(__file__).resolve().parent / "chrome_extension"
CHECK_LIST2_FILE = Path(__file__).resolve().parent / "check_list2.txt"
CHECK_LIST2_JSON = Path(__file__).resolve().parent / "check_list2.json"
COVERS_DIR = Path(__file__).resolve().parent / "covers"
PENDING_ROOT = Path(__file__).resolve().parent / "pending"
_COVER_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,40}$")
_MAX_COVER_BYTES = 8 * 1024 * 1024
CHECK_LIST1_FILE = Path(__file__).resolve().parent / "check_list1.txt"
CHECK_LIST1_JSON = Path(__file__).resolve().parent / "check_list1.json"
DOWNLOADED_JAV_FILE = Path(__file__).resolve().parent / "downloaded_jav.txt"
UNKNOWN_JAV_FOLDER = "_UNKNOWN"
# 2026-10-21: 入库流程对接 javsp 刮削，片库内不再按番号前缀分文件夹。
# 识别出番号的成片统一平铺到 _incoming/，由 javsp 刮削后移入 JAV/{女优}/[番号]/。
INCOMING_JAV_FOLDER = "_incoming"
# 与 organize_jav_files 一致：字母前缀 + (-|_) + 数字
CODE_PREFIX_RE = re.compile(r"([A-Za-z]+)[-_]\d+")
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m4v"}
)

# 兼容旧文件名
CHECK_LIST_FILE = CHECK_LIST2_FILE
CHECK_LIST_JSON = CHECK_LIST2_JSON
_LEGACY_CHECK_LIST_FILE = Path(__file__).resolve().parent / "check_list.txt"
_LEGACY_CHECK_LIST_JSON = Path(__file__).resolve().parent / "check_list.json"

CHECKLIST2_STATUSES = frozenset(
    {
        "pending",
        "incomplete",
        "ready",
        "downloading",
        "download_done",
        "failed",
        "downloaded",
    }
)
# 采集时勿覆盖，避免把进行中的任务重置回 ready
CHECKLIST2_PROGRESS_STATUSES = frozenset(
    {"downloaded", "downloading", "download_done"}
)


def ensure_state_files() -> list[str]:
    """
    若本机清单 / 已下载名单不存在则创建空文件。
    返回新创建的文件名列表。
    """
    created: list[str] = []
    specs: list[tuple[Path, str]] = [
        (CHECK_LIST2_JSON, "[]\n"),
        (CHECK_LIST2_FILE, ""),
        (CHECK_LIST1_JSON, "[]\n"),
        (CHECK_LIST1_FILE, ""),
        (_LEGACY_CHECK_LIST_JSON, "[]\n"),
        (_LEGACY_CHECK_LIST_FILE, ""),
        (DOWNLOADED_JAV_FILE, ""),
    ]
    for path, body in specs:
        if path.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        created.append(path.name)
    return created


def load_downloaded_jav(
    path: Path | str = DOWNLOADED_JAV_FILE,
) -> set[str]:
    """读取已下载番号集合（归一化后）。"""
    file_path = Path(path)
    if not file_path.is_file():
        return set()
    codes: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        codes.add(normalize_code(text))
    return codes


def is_code_downloaded(code: str | None, downloaded: set[str]) -> bool:
    if not code:
        return False
    return normalize_code(code) in downloaded


def append_downloaded_jav(
    code: str,
    path: Path | str = DOWNLOADED_JAV_FILE,
) -> bool:
    """追加番号到名单（已存在则跳过）。返回是否真正写入。"""
    cleaned = code.strip().upper()
    if not cleaned:
        return False
    key = normalize_code(cleaned)
    file_path = Path(path)
    existing = load_downloaded_jav(file_path)
    if key in existing:
        return False
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(cleaned + "\n")
    return True


def sync_downloaded_jav_from_dir(
    jav_dir: Path | str,
    path: Path | str = DOWNLOADED_JAV_FILE,
) -> tuple[int, int, int]:
    """
    扫描本地片库目录，重写 downloaded_jav.txt。
    返回 (写入番号数, 视频文件数, 未能抽番号的文件数)。
    """
    root = Path(jav_dir)
    if not root.is_dir():
        raise TabCheckError(f"片库目录不存在: {root}")

    by_norm: dict[str, str] = {}
    video_count = 0
    no_code = 0
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        video_count += 1
        code = _extract_code(file_path.name)
        if not code:
            no_code += 1
            continue
        key = normalize_code(code)
        # 优先保留带连字符的写法
        prev = by_norm.get(key)
        if prev is None or ("-" in code and "-" not in prev):
            by_norm[key] = code

    codes = sorted(by_norm.values(), key=lambda c: (normalize_code(c), c))
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(codes) + ("\n" if codes else ""), encoding="utf-8")
    return len(codes), video_count, no_code


def extract_code_prefix(code: str) -> str:
    """番号字母前缀（如 SSNI-126 → SSNI）。仅保留作展示/日志用途。

    2026-10-21 起入库不再按前缀建子目录，见 move_mp4_to_jav_library。
    """
    match = CODE_PREFIX_RE.search(code.strip())
    if match:
        return match.group(1).upper()
    return UNKNOWN_JAV_FOLDER


def pending_dir_for_code(code: str) -> Path:
    """待下载目录：项目 pending/{番号}/。采集写元数据，下载再放入成片并搬走。"""
    cleaned = safe_path_component(str(code or "").strip().upper(), max_length=80)
    if cleaned in {"", "_unnamed"}:
        raise TabCheckError(f"无法为番号创建待下载目录: {code!r}")
    return PENDING_ROOT / cleaned


def move_mp4_to_jav_library(
    mp4_path: Path | str,
    code: str,
    jav_root: Path | str,
) -> Path:
    """
    将合成后的 mp4 挪到片库 {jav_root}/_incoming/（2026-10-21 起不再按前缀分目录）。

    入库后的刮削归 javsp 负责：识别番号 → JAV/{女优}/[番号] 标题/，
    识别不出番号 → javsp 会自动移到 {jav_root}/_UNKNOWN/。
    目标同名已存在则先删除再移动。
    """
    src = Path(mp4_path)
    if not src.is_file():
        raise TabCheckError(f"待入库文件不存在: {src}")

    root = Path(jav_root)
    root.mkdir(parents=True, exist_ok=True)
    dest_dir = root / INCOMING_JAV_FOLDER
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    root_abs = root.resolve()
    dest_abs = dest.resolve()
    try:
        dest_abs.relative_to(root_abs)
    except ValueError as exc:
        raise TabCheckError(f"安全拦截：入库目标不在片库内: {dest_abs}") from exc

    if dest.exists():
        if dest.is_dir():
            raise TabCheckError(f"入库目标是目录，拒绝覆盖: {dest}")
        dest.unlink()

    shutil.move(str(src), str(dest))
    return dest


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


def quality_rank(q: str | None) -> int:
    """720p → 720；1280x720 → 用高度 720；无法解析 → 0。"""
    s = str(q or "").strip().lower()
    if not s:
        return 0
    m = re.search(r"(\d+)\s*p\b", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*x\s*(\d+)", s)
    if m:
        return int(m.group(2))
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else 0


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
    metadata: dict = field(default_factory=dict)

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
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            page_url=page_url,
            title=title,
            video_uuid=video_uuid,
            uuids=uuids,
            m3u8_url=m3u8_url,
            quality=quality,
            metadata=metadata,
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
    metadata: dict = field(default_factory=dict)
    pending_dir: str = ""

    def to_tab_item(self) -> TabItem:
        return TabItem(
            page_url=self.page_url,
            title=self.title or self.page_url,
            video_uuid=self.video_uuid,
            uuids=self.uuids,
            m3u8_url=self.m3u8_url,
            quality=self.quality,
            metadata=self.metadata,
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
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            page_url=page_url,
            code=str(code).upper() if code else extract_code_from_url(page_url),
            title=str(data.get("title", "")).strip() or page_url,
            video_uuid=video_uuid,
            uuids=uuids or None,
            m3u8_url=str(data.get("m3u8_url", "")).strip() or None,
            quality=str(data.get("quality", "")).strip() or None,
            status=status,
            metadata=metadata,
            pending_dir=str(data.get("pending_dir") or "").strip(),
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
                metadata=item.metadata or {},
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


def _short_text(text: str, width: int = 48) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _snapshot_from_report(
    item: TabItem,
    prev: CheckList2Entry | None,
    code: str | None,
) -> tuple[dict, str, str]:
    """返回 (metadata, pending_dir, status)。

    资料齐全才写入 pending 并标 ready。空上报不覆盖已有定稿。
    进行中的任务不会进到这里。
    """
    new_meta = dict(item.metadata or {})
    if code and not str(new_meta.get("code") or "").strip():
        new_meta["code"] = code
    if item.page_url and not str(new_meta.get("source_url") or "").strip():
        new_meta["source_url"] = item.page_url
    if item.title and not str(new_meta.get("title") or "").strip():
        new_meta["title"] = item.title

    prev_complete = bool(
        prev
        and prev.pending_dir
        and not missing_collect_fields(prev.metadata, page_url=prev.page_url, code=prev.code or "")
    )
    if not item.metadata or (prev_complete and missing_collect_fields(
        new_meta, page_url=item.page_url, page_title=item.title, code=code or ""
    )):
        if prev_complete and prev is not None:
            return dict(prev.metadata), prev.pending_dir, "ready"
        kept = dict(prev.metadata) if prev and not item.metadata else new_meta
        return kept, "", "incomplete"

    missing = missing_collect_fields(
        new_meta, page_url=item.page_url, page_title=item.title, code=code or ""
    )
    if missing:
        return new_meta, "", "incomplete"

    try:
        pending = pending_dir_for_code(code or new_meta.get("code") or "")
    except TabCheckError:
        return new_meta, "", "incomplete"
    try:
        meta = prepare_pending_dir(
            pending,
            new_meta,
            {
                "page_url": item.page_url,
                "video_uuid": item.video_uuid,
                "uuids": item.uuids or [],
                "m3u8_url": item.m3u8_url or "",
                "quality": item.quality or "",
            },
            page_url=item.page_url,
            page_title=item.title,
        )
    except MetadataPackageError:
        raise
    rel = pending.resolve().relative_to(PENDING_ROOT.parent.resolve()).as_posix()
    return meta, rel, "ready"


def _merge_extension_items(
    tab_items: list[TabItem],
    *,
    by_url: dict[str, CheckList2Entry],
    output_path: Path,
    downloaded: set[str] | None = None,
    report_seq: list[int] | None = None,
) -> tuple[int, list[str]]:
    """把扩展上报条目合并进 by_url 并落盘；返回 (新增/更新数, 跳过 URL 列表)。

    资料齐全时写入 pending/{番号}/ 并标 ready。缺字段标 incomplete，不进入下载。
    下载中的条目保持原样。空上报不擦掉已有定稿。
    """
    skipped: list[str] = []
    added_or_updated = 0
    status_touched = False
    downloaded_set = downloaded if downloaded is not None else load_downloaded_jav()

    def _next_seq() -> int:
        if report_seq is None:
            return 0
        report_seq[0] += 1
        return report_seq[0]

    for item in tab_items:
        if not item.video_uuid:
            continue
        key = item.page_url.rstrip("/")
        raw_code = ""
        if isinstance(item.metadata, dict):
            raw_code = str(item.metadata.get("code") or "").strip().upper()
        code = raw_code or extract_code_from_url(item.page_url)
        prev = by_url.get(key)
        code_label = code or (prev.code if prev else None) or "?"
        title_hint = _short_text(item.title or item.page_url)
        quality_hint = item.quality or "?"

        if is_code_downloaded(code, downloaded_set):
            skipped.append(item.page_url)
            seq = _next_seq()
            prefix = f"#{seq} " if seq else ""
            try:
                metadata, pending_dir, snap_status = _snapshot_from_report(item, prev, code)
            except MetadataPackageError as exc:
                print(
                    f"  {prefix}[本地已有] {code_label}  资料未刷新：{exc}  | {title_hint}",
                    flush=True,
                )
                if prev and prev.status != "downloaded":
                    prev.status = "downloaded"
                    status_touched = True
                continue
            entry = CheckList2Entry(
                page_url=item.page_url,
                code=code or (prev.code if prev else None),
                title=str(metadata.get("title") or item.title or item.page_url),
                video_uuid=(prev.video_uuid if prev and prev.video_uuid else item.video_uuid),
                uuids=(prev.uuids if prev and prev.uuids else item.uuids),
                m3u8_url=(prev.m3u8_url if prev and prev.m3u8_url else item.m3u8_url),
                quality=(prev.quality if prev and prev.quality else item.quality),
                status="downloaded",
                metadata=metadata,
                pending_dir=pending_dir,
            )
            by_url[key] = entry
            status_touched = True
            where = pending_dir or "未写入"
            if snap_status == "ready":
                print(
                    f"  {prefix}[本地已有] {code_label}  不重新下载，资料已写入 {where}  | {title_hint}",
                    flush=True,
                )
            else:
                missing = missing_collect_fields(
                    metadata, page_url=item.page_url, page_title=item.title, code=code or ""
                )
                detail = "、".join(missing) if missing else "资料不全"
                print(
                    f"  {prefix}[本地已有] {code_label}  不重新下载，资料仍缺 {detail}  | {title_hint}",
                    flush=True,
                )
            continue

        if prev and prev.status in CHECKLIST2_PROGRESS_STATUSES:
            skipped.append(item.page_url)
            seq = _next_seq()
            prefix = f"#{seq} " if seq else ""
            print(
                f"  {prefix}[清单已有] {code_label}  [{prev.status}]，跳过  | {title_hint}",
                flush=True,
            )
            continue

        keep_media = False
        if (
            prev
            and prev.video_uuid == item.video_uuid
            and prev.m3u8_url
            and item.m3u8_url
            and prev.quality
            and item.quality
        ):
            prev_q = quality_rank(prev.quality)
            new_q = quality_rank(item.quality)
            if prev_q and new_q and prev_q >= new_q:
                keep_media = True
                skipped.append(item.page_url)
                seq = _next_seq()
                prefix = f"#{seq} " if seq else ""
                print(
                    f"  {prefix}[清晰度忽略] {code_label}  {quality_hint} ≤ 现有 {prev.quality}"
                    f"  | {title_hint}",
                    flush=True,
                )

        try:
            metadata, pending_dir, status = _snapshot_from_report(item, prev, code)
        except MetadataPackageError as exc:
            metadata = dict(item.metadata or {})
            pending_dir = ""
            status = "incomplete"
            seq = _next_seq()
            prefix = f"#{seq} " if seq else ""
            print(
                f"  {prefix}[资料未定稿] {code_label}  {exc}  | {title_hint}",
                flush=True,
            )
        else:
            if status == "incomplete":
                missing = missing_collect_fields(
                    metadata,
                    page_url=item.page_url,
                    page_title=item.title,
                    code=code or "",
                )
                seq = _next_seq()
                prefix = f"#{seq} " if seq else ""
                detail = "、".join(missing) if missing else "资料不全"
                print(
                    f"  {prefix}[资料不全] {code_label}  缺 {detail}  | {title_hint}",
                    flush=True,
                )

        normalized_title = str(metadata.get("title") or item.title or "").strip()
        entry = CheckList2Entry(
            page_url=item.page_url,
            code=code or (prev.code if prev else None),
            title=normalized_title or item.page_url,
            video_uuid=(prev.video_uuid if keep_media and prev else item.video_uuid),
            uuids=(prev.uuids if keep_media and prev else item.uuids),
            m3u8_url=(prev.m3u8_url if keep_media and prev else item.m3u8_url),
            quality=(prev.quality if keep_media and prev else item.quality),
            status=status,
            metadata=metadata,
            pending_dir=pending_dir,
        )
        by_url[key] = entry
        added_or_updated += 1
        if not keep_media:
            action = "更新" if prev else "新增"
            seq = _next_seq()
            prefix = f"#{seq} " if seq else ""
            m3u8_hint = quality_hint if item.m3u8_url else "待探测"
            print(
                f"  {prefix}[{action}] {code_label}  → {status}  ({m3u8_hint})"
                f"  | {title_hint}",
                flush=True,
            )

    if added_or_updated or status_touched:
        kept_items = list(by_url.values())
        write_check_list2(
            kept_items,
            json_file=output_path,
            txt_file=output_path.with_suffix('.txt'),
        )
        list1_entries = [
            List1Entry(page_url=e.page_url, code=e.code)
            for e in kept_items
            if e.status != "downloaded"
        ]
        write_check_list1(list1_entries)

    return added_or_updated, skipped


def _cover_suffix(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_cover_bytes(code: str, data: bytes) -> str:
    """把浏览器已下载的封面写入 covers/{番号}.ext，返回相对路径。"""
    normalized = str(code or "").strip().upper()
    if not _COVER_CODE_RE.match(normalized):
        raise TabCheckError(f"封面番号不合法: {code!r}")
    if not data or len(data) > _MAX_COVER_BYTES:
        raise TabCheckError(f"封面大小无效: {len(data) if data else 0}")
    suffix = _cover_suffix(data)
    if suffix is None:
        raise TabCheckError("封面不是 jpeg/png/gif/webp")

    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    final = COVERS_DIR / f"{normalized}{suffix}"
    fd, tmp_name = tempfile.mkstemp(dir=str(COVERS_DIR), prefix=".cover_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, final)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return final.relative_to(COVERS_DIR.parent).as_posix()


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
    downloaded = load_downloaded_jav()
    if downloaded:
        print(f"已加载本地片库名单: {len(downloaded)} 个番号（将自动跳过）")

    state_lock = threading.Lock()
    stop_event = threading.Event()
    last_activity = threading.Event()
    last_activity.set()
    total_updated = 0
    all_skipped: list[str] = []
    session_ready = 0
    report_seq = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal total_updated, session_ready
            path = urlparse(self.path).path
            if path == "/cover":
                self._save_cover()
                return
            if path not in ("/export", "/tabs"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self.send_error(400)
                return
            items = [i for i in _parse_post_body(body) if i.video_uuid]
            if not items:
                print("  [!] 收到上报，但无有效 video_uuid，已忽略", flush=True)
                updated, skipped = 0, []
            else:
                with state_lock:
                    updated, skipped = _merge_extension_items(
                        items,
                        by_url=by_url,
                        output_path=output_path,
                        downloaded=downloaded,
                        report_seq=report_seq,
                    )
                    total_updated += updated
                    all_skipped.extend(skipped)
                    session_ready = sum(
                        1 for e in by_url.values() if e.status == "ready"
                    )
                print(
                    f"  ── 本批 +{updated} / 跳过 {len(skipped)}"
                    f"  │ 会话累计新增 {total_updated}"
                    f"  │ 清单 ready={session_ready}",
                    flush=True,
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

        def _save_cover(self) -> None:
            query = urlparse(self.path).query
            code = ""
            for part in query.split("&"):
                if part.startswith("code="):
                    code = unquote(part[5:])
                    break
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > _MAX_COVER_BYTES:
                self.send_error(400, "封面大小无效")
                return
            data = self.rfile.read(length)
            try:
                rel = save_cover_bytes(code, data)
            except TabCheckError as exc:
                payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            print(f"  [封面] {code.strip().upper()} → {rel}", flush=True)
            payload = json.dumps({"ok": True, "cover_file": rel}).encode("utf-8")
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
    print(
        "每条上报会即时打印：[新增]/[更新] / [本地已有] / [清单已有] / [清晰度忽略]"
    )
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
    incomplete = sum(1 for e in kept_items if e.status == "incomplete")
    print(
        f"\n清单共 {len(kept_items)} 条 → {output_path}"
        f"（本次新增/更新 {total_updated}，ready={ready}，incomplete={incomplete}）"
    )
    print(f"待下载目录: {PENDING_ROOT}")
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
