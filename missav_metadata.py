#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MissAV 元数据规范化、NFO 生成、封面下载与电影打包工具。

含元数据规范化、Kodi/Jellyfin 兼容 NFO 生成、封面图片下载与右侧裁剪（poster/fanart）、
metadata.json 写入，以及将 mp4 打包到 JAV/{女优}/[番号] 标题/ 目录。
依赖 Pillow 处理图像、urllib 下载封面、ffprobe 探测时长；供本机与 NAS 端共用。
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_CODE_PREFIX_RE = re.compile(r'^\s*(?:FC2-PPV-\d{6,8}|HEYZO-\d{4}|[A-Z]{2,10}-\d{2,5}(?:-\d+)?|[A-Z]{2,10}\d{2,5})\b\s*',re.IGNORECASE)
_MISSAV_SUFFIX_RE = re.compile(r'\s*-\s*MissAV\b.*$', re.IGNORECASE)
_TRAILING_DOT_SPACE_RE = re.compile(r'[\s\.]+$')
_MULTISPACE_RE = re.compile(r'\s+')
_ILLEGAL_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _dedupe(items: Iterable[Any]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for it in items:
        if it is None:
            continue
        s = str(it).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


_PAREN_CONTENT_RE = re.compile(r'\s*[(（][^()（）]*[)）]')


def _strip_paren_content(value: str) -> str:
    """去掉圆括号及其中的别名，保留括号前的名字。"""
    s = _PAREN_CONTENT_RE.sub(' ', str(value or ''))
    s = _MULTISPACE_RE.sub(' ', s).strip(' ,，、')
    return s


def _actress_names(value: Any) -> List[str]:
    names: List[str] = []
    for item in _as_list(value):
        stripped = _strip_paren_content(item)
        parts = re.split(r'[,，、]', stripped) if re.search(r'[,，、]', stripped) else [stripped]
        names.extend(parts)
    return _dedupe(_strip_paren_content(part) for part in names)


def _as_list(value: Any) -> List[Any]:
    """列表字段归一：None 为空列表；字符串按整体作为单元素列表（不按字符拆分）；其它可迭代对象展开。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def normalize_metadata(raw: dict, page_url: str = '', page_title: str = '', code: str = '') -> dict:
    """规范 MissAV 页提取的原始元数据；缺字段稳健，列表去重去空，code 大写，保留 extra。"""
    if not isinstance(raw, dict):
        raw = {}
    code = str(code or '').strip().upper()
    if not code and raw.get('code'):
        code = str(raw.get('code')).strip().upper()
    return {
        'title': str(raw.get('title') or page_title or '').strip(),
        'original_title': str(raw.get('original_title') or '').strip(),
        'code': code,
        'actresses': _actress_names(raw.get('actresses')),
        'male_actors': _dedupe(_as_list(raw.get('male_actors'))),
        'directors': _dedupe(_as_list(raw.get('directors'))),
        'makers': _dedupe(_as_list(raw.get('makers'))),
        'labels': _dedupe(_as_list(raw.get('labels'))),
        'series': _dedupe(_as_list(raw.get('series'))),
        'genres': _dedupe(_as_list(raw.get('genres'))),
        'release_date': str(raw.get('release_date') or '').strip(),
        'runtime': raw.get('runtime'),
        'plot': str(raw.get('plot') or '').strip(),
        'cover_url': str(raw.get('cover_url') or '').strip(),
        'cover_file': str(raw.get('cover_file') or '').strip(),
        'preview_urls': _dedupe(_as_list(raw.get('preview_urls'))),
        'source_url': str(page_url or raw.get('source_url') or '').strip(),
        'page_title': str(page_title or raw.get('page_title') or '').strip(),
        'extra': raw.get('extra') if isinstance(raw.get('extra'), dict) else {},
    }


def clean_movie_title(title: str, code: str = '') -> str:
    """去开头番号（未传 code 时自动识别开头可识别番号）和末尾 - MissAV...，清理空白。

    仅处理开头：传入 code 只在标题开头移除该 code，不动标题中间相同文本。
    """
    t = str(title or '').strip()
    code = str(code or '').strip().upper()
    if code:
        t = re.sub(
            r'^\s*' + re.escape(code) + r'\s*[-–—:_ ]*', '', t, count=1, flags=re.IGNORECASE
        ).strip()
    else:
        m = _CODE_PREFIX_RE.match(t)
        if m:
            t = t[m.end():].strip()
    t = _MISSAV_SUFFIX_RE.sub('', t).strip()
    t = _MULTISPACE_RE.sub(' ', t).strip(' -_')
    return t


_RESERVED_BASES = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}


def _truncate_utf8_bytes(s: str, max_bytes: int) -> str:
    """逐字符安全截断，使 UTF-8 编码字节数不超过 max_bytes；再次去尾部点与空格。"""
    if max_bytes <= 0:
        return ''
    encoded = s.encode('utf-8', errors='ignore')
    if len(encoded) <= max_bytes:
        return s
    out_chars: List[str] = []
    used = 0
    for ch in s:
        ch_bytes = ch.encode('utf-8', errors='ignore')
        if used + len(ch_bytes) > max_bytes:
            break
        out_chars.append(ch)
        used += len(ch_bytes)
    s = ''.join(out_chars)
    s = _TRAILING_DOT_SPACE_RE.sub('', s).strip()
    return s


def safe_path_component(value: str, max_length: int = 120) -> str:
    """Windows/NAS 文件名安全：保留中文，替换非法字符，去尾部点与空格，避开 Windows 保留名；
    并限制 UTF-8 编码字节数不超过 240（防止长中文文件名超过 NAS 255 字节）。"""
    s = _ILLEGAL_FS_CHARS.sub('_', str(value or ''))
    s = s.strip()
    s = _TRAILING_DOT_SPACE_RE.sub('', s).strip()
    if not s:
        s = '_unnamed'
    base = s.rsplit('.', 1)[0].upper()
    if base in _RESERVED_BASES:
        s = '_' + s
    if max_length and len(s) > max_length:
        s = s[:max_length]
        s = _TRAILING_DOT_SPACE_RE.sub('', s).strip()
    s = _truncate_utf8_bytes(s, 240)
    return s


def movie_folder_parts(metadata: dict, code: str = '', page_title: str = '') -> Tuple[str, str, str]:
    """返回 (actress_folder, movie_folder, display_title)。

    女优逗号连接，缺女优 '_UNKNOWN'；目录 '[CODE] 清理后的标题'；显示标题 'CODE 清理后的标题'。
    actress_folder / movie_folder 均经 safe_path_component 处理并保留非 ASCII。
    """
    if not isinstance(metadata, dict):
        metadata = {}
    code = str(code or metadata.get('code') or '').strip().upper()
    actresses = metadata.get('actresses') or []
    actress_folder = safe_path_component(
        ','.join(str(a).strip() for a in actresses if str(a).strip()) or '_UNKNOWN', max_length=100,
    )
    title = str(metadata.get('title') or page_title or '').strip()
    cleaned = clean_movie_title(title, code)
    if code and cleaned:
        display = cleaned if cleaned.upper().startswith(code) else f'{code} {cleaned}'
        movie_folder = f'[{code}] {cleaned}'
    elif cleaned:
        display = cleaned
        movie_folder = cleaned
    else:
        display = code or '未知标题'
        movie_folder = display
    return actress_folder, safe_path_component(movie_folder, max_length=180), display


def build_nfo(metadata: dict, code: str = '', page_title: str = '', runtime=None) -> str:
    """生成 Kodi/Jellyfin 兼容 NFO UTF-8 字符串（movie 根，正确转义，不虚构评分或 tmdbid）。"""
    if not isinstance(metadata, dict):
        metadata = {}
    code = str(code or metadata.get('code') or '').strip().upper()
    _, _, title = movie_folder_parts(metadata, code=code, page_title=page_title)
    title = title or code or '未知标题'
    original_title = str(metadata.get('original_title') or '').strip()
    plot = str(metadata.get('plot') or '').strip()
    premiered = str(metadata.get('release_date') or '').strip()

    studio = ''
    for cand in (metadata.get('makers') or []) + (metadata.get('labels') or []):
        if str(cand).strip():
            studio = str(cand).strip()
            break
    director = ''
    directors = metadata.get('directors') or []
    if directors:
        director = str(directors[0]).strip()

    series = metadata.get('series') or []
    set_name = str(series[0]).strip() if series else ''

    actresses = metadata.get('actresses') or []
    male_actors = metadata.get('male_actors') or []
    genres = metadata.get('genres') or []
    labels = metadata.get('labels') or []
    tags = _dedupe(
        [str(g).strip() for g in list(genres) + list(labels) if str(g).strip()]
    )

    movie = ET.Element('movie')
    ET.SubElement(movie, 'title').text = title
    if original_title:
        ET.SubElement(movie, 'originaltitle').text = original_title
    if plot:
        ET.SubElement(movie, 'plot').text = plot
    rt = runtime if runtime is not None else metadata.get('runtime')
    if rt is not None:
        try:
            rt_min = int(float(rt))
        except (TypeError, ValueError):
            rt_min = 0
        if rt_min > 0:
            ET.SubElement(movie, 'runtime').text = str(rt_min)
    ET.SubElement(movie, 'mpaa').text = 'NC-17'
    if code:
        uid = ET.SubElement(movie, 'uniqueid')
        uid.set('type', 'num')
        uid.set('default', 'true')
        uid.text = code
    for g in genres:
        ET.SubElement(movie, 'genre').text = str(g)
    for tg in tags:
        ET.SubElement(movie, 'tag').text = str(tg)
    if premiered:
        ET.SubElement(movie, 'premiered').text = premiered
    if studio:
        ET.SubElement(movie, 'studio').text = studio
    if director:
        ET.SubElement(movie, 'director').text = director
    if set_name:
        set_el = ET.SubElement(movie, 'set')
        ET.SubElement(set_el, 'name').text = set_name
    for a in actresses:
        actor = ET.SubElement(movie, 'actor')
        ET.SubElement(actor, 'name').text = str(a)
        ET.SubElement(actor, 'type').text = 'Actress'
    for a in male_actors:
        actor = ET.SubElement(movie, 'actor')
        ET.SubElement(actor, 'name').text = str(a)
        ET.SubElement(actor, 'type').text = 'Actor'

    buf = BytesIO()
    ET.ElementTree(movie).write(buf, encoding='utf-8', xml_declaration=True)
    return buf.getvalue().decode('utf-8')


def write_metadata_json(path, metadata: dict, scrape_needed: bool = False, errors=None) -> Path:
    """原子写入 JSON 元数据；source='missav'，含规范 metadata、scrape_needed、errors。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'source': 'missav',
        'scrape_needed': bool(scrape_needed),
        'errors': list(errors) if errors is not None else [],
        'metadata': normalize_metadata(metadata) if isinstance(metadata, dict) else metadata,
    }
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix='.metadata_tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, p)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return p


def probe_runtime_minutes(video_path) -> Optional[int]:
    """用 ffprobe 探测主视频流时长（分钟）；失败返回 None。"""
    try:
        out = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'format=duration', '-of',
                'default=noprint_wrappers=1:nokey=1', str(video_path),
            ],
            capture_output=True, text=True, check=True, timeout=30,
        )
        dur = out.stdout.strip()
        if not dur:
            return None
        return int(round(float(dur) / 60))
    except Exception:
        return None


class MetadataPackageError(Exception):
    """打包电影时发生的不可恢复错误（如最终目录已存在）。"""


@dataclass
class PackageResult:
    final_dir: Path
    video_path: Path
    scrape_needed: bool
    errors: list


def create_images(cover_source: str | Path, output_dir: str | Path) -> dict:
    """Pillow 打开图像：完整图写 fanart.jpg；横版按 x=int(width*0.528) 裁右侧写 poster.jpg，非横版完整图作为 poster。

    输出目录自建。RGB JPEG quality=90。返回 {'fanart': Path, 'poster': Path}。
    """
    from PIL import Image

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(str(cover_source)) as im:
        im = im.convert('RGB')
        width, height = im.size

        fanart_path = out_dir / 'fanart.jpg'
        im.save(str(fanart_path), 'JPEG', quality=90)

        is_landscape = width > height
        if is_landscape:
            left = int(width * 0.528)
            poster_im = im.crop((left, 0, width, height))
        else:
            poster_im = im
        poster_path = out_dir / 'poster.jpg'
        poster_im.save(str(poster_path), 'JPEG', quality=90)

    return {'fanart': fanart_path, 'poster': poster_path}


def download_cover(url: str, output_dir: str | Path, timeout: int = 30) -> Path:
    """仅 http/https，带浏览器 User-Agent 下载封面到 output_dir/cover_source。

    临时文件成功后原子替换，失败清理。返回保存后的 Path。不做真实网络断言。
    """
    if not isinstance(url, str) or not url.lower().startswith(('http://', 'https://')):
        raise MetadataPackageError(f'不支持的封面 URL 协议: {url!r}')

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 固定写 cover_source，不依赖 URL basename（避免特殊URL路径和中间文件命名问题）
    final_path = out_dir / 'cover_source'

    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'},
    )

    fd, tmp_path = tempfile.mkstemp(dir=str(out_dir), prefix='.cover_tmp_', suffix='.part')
    fd_owned = True
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if not data:
            raise MetadataPackageError('下载封面返回空字节')
        with os.fdopen(fd, 'wb') as handle:
            fd_owned = False
            handle.write(data)
        os.replace(tmp_path, final_path)
    except MetadataPackageError:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    except Exception as exc:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise MetadataPackageError(f'下载封面失败: {url}') from exc
    return final_path


def _safe_staging_dir(base_dir: Path) -> Path:
    """在 base_dir 下创建唯一的 staging 目录。"""
    import uuid

    for _ in range(10):
        cand = base_dir / f'.staging_{uuid.uuid4().hex[:12]}'
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    raise MetadataPackageError('无法创建唯一 staging 目录')


def _unique_child(parent: Path, name: str) -> Path:
    """若 parent/name 已存在，追加后缀直到唯一。"""
    cand = parent / name
    if not cand.exists():
        return cand
    stem = Path(name).stem
    suffix = Path(name).suffix
    for i in range(2, 1000):
        cand = parent / f'{stem}_{i}{suffix}'
        if not cand.exists():
            return cand
    raise MetadataPackageError(f'无法为 {name} 分配唯一文件名')


def _resolve_saved_cover(cover_file: str) -> Optional[Path]:
    """解析采集时保存的 covers/{番号}.jpg。路径必须落在项目 covers 目录内。"""
    rel = str(cover_file or '').strip().replace('\\', '/')
    if not rel or rel.startswith('/') or '..' in rel.split('/'):
        return None
    root = Path(__file__).resolve().parent
    path = (root / rel).resolve()
    try:
        path.relative_to((root / 'covers').resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


_COLLECT_REQUIRED = (
    ('code', '番号'),
    ('title', '标题'),
    ('actresses', '女优'),
    ('genres', '类型'),
    ('release_date', '发行日期'),
    ('plot', '简介'),
    ('makers', '发行商'),
    ('cover', '封面'),
)


def missing_collect_fields(raw: dict, page_url: str = '', page_title: str = '', code: str = '') -> List[str]:
    """采集定稿所缺字段的中文名。封面认 cover_file 或 cover_url。"""
    if not isinstance(raw, dict):
        raw = {}
    meta = normalize_metadata(raw, page_url=page_url, page_title=page_title, code=code or raw.get('code') or '')
    missing: List[str] = []
    for key, label in _COLLECT_REQUIRED:
        if key == 'cover':
            if not (meta.get('cover_file') or meta.get('cover_url')):
                missing.append(label)
            continue
        value = meta.get(key)
        if isinstance(value, list):
            if not value:
                missing.append(label)
        elif not str(value or '').strip():
            missing.append(label)
    return missing


def prepare_pending_dir(pending_dir, metadata, job: dict, *, page_url: str = '', page_title: str = '') -> dict:
    """把 NFO、封面、metadata.json、job.json 写进待下载目录。

    不放视频。已有 mp4 会保留。资料不全或封面失败时抛 MetadataPackageError，不改已有目录。
    返回规范化后的 metadata。
    """
    import shutil

    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(job, dict):
        job = {}
    meta = normalize_metadata(
        metadata,
        page_url=page_url or metadata.get('source_url') or '',
        page_title=page_title or metadata.get('title') or '',
        code=metadata.get('code') or '',
    )
    missing = missing_collect_fields(meta)
    if missing:
        raise MetadataPackageError('资料不全: ' + '、'.join(missing))

    pending_dir = Path(pending_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)
    staging = _safe_staging_dir(pending_dir.parent)

    try:
        saved = _resolve_saved_cover(str(meta.get('cover_file') or ''))
        if saved:
            cover_path = staging / 'cover_source'
            shutil.copyfile(saved, cover_path)
        elif meta.get('cover_url'):
            cover_path = download_cover(str(meta['cover_url']), staging)
        else:
            raise MetadataPackageError('缺少封面')
        create_images(cover_path, staging)
        try:
            if cover_path.exists():
                cover_path.unlink()
        except OSError:
            pass

        code = str(meta.get('code') or '').strip().upper()
        nfo = build_nfo(meta, code=code, page_title=str(meta.get('title') or page_title or ''))
        (staging / 'movie.nfo').write_text(nfo, encoding='utf-8')
        write_metadata_json(staging / 'metadata.json', meta, scrape_needed=False, errors=[])
        job_payload = {
            'page_url': str(job.get('page_url') or meta.get('source_url') or ''),
            'code': code,
            'title': str(meta.get('title') or ''),
            'video_uuid': str(job.get('video_uuid') or ''),
            'uuids': list(job.get('uuids') or []),
            'm3u8_url': str(job.get('m3u8_url') or ''),
            'quality': str(job.get('quality') or ''),
        }
        (staging / 'job.json').write_text(
            json.dumps(job_payload, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )

        for item in staging.iterdir():
            target = pending_dir / item.name
            if target.exists():
                target.unlink()
            shutil.move(str(item), str(target))
    except MetadataPackageError:
        raise
    except Exception as exc:
        raise MetadataPackageError(f'准备待下载目录失败: {exc}') from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return meta


def shelve_pending_dir(pending_dir, mp4_path, library_root) -> PackageResult:
    """把成片放进待下载目录，用 ffprobe 补时长，再整夹搬到片库。

    片库目标已存在时拒绝覆盖，待下载目录和成片都留在原地。
    """
    import shutil

    pending_dir = Path(pending_dir)
    mp4_path = Path(mp4_path)
    library_root = Path(library_root)
    meta_file = pending_dir / 'metadata.json'
    nfo_file = pending_dir / 'movie.nfo'
    if not meta_file.is_file() or not nfo_file.is_file():
        raise MetadataPackageError(f'待下载目录不完整: {pending_dir}')
    if not mp4_path.is_file():
        raise MetadataPackageError(f'待入库文件不存在: {mp4_path}')

    payload = json.loads(meta_file.read_text(encoding='utf-8'))
    metadata = payload.get('metadata') if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        raise MetadataPackageError(f'metadata.json 缺少 metadata: {meta_file}')
    metadata = normalize_metadata(metadata, code=metadata.get('code') or '')
    code = str(metadata.get('code') or '').strip().upper()

    runtime = probe_runtime_minutes(mp4_path)
    if runtime:
        metadata['runtime'] = runtime

    video_name = f'{code}.mp4' if code else safe_path_component(mp4_path.name, max_length=120)
    nfo = build_nfo(
        metadata,
        code=code,
        page_title=str(metadata.get('page_title') or metadata.get('title') or ''),
        runtime=metadata.get('runtime'),
    )
    nfo_file.write_text(nfo, encoding='utf-8')
    write_metadata_json(pending_dir / 'metadata.json', metadata, scrape_needed=False, errors=[])

    actress_folder, movie_folder, _ = movie_folder_parts(metadata, code=code)
    final_dir = library_root / 'JAV' / actress_folder / movie_folder
    if final_dir.exists():
        raise MetadataPackageError(f'最终目录已存在，拒绝覆盖: {final_dir}')

    # 大文件留在下载盘上组好目录，再整夹搬进片库，避免先拷到项目所在的固态盘。
    staging = _safe_staging_dir(mp4_path.parent)
    try:
        dest_video = staging / video_name
        if mp4_path.resolve() != dest_video.resolve():
            shutil.move(str(mp4_path), str(dest_video))
        for name in ('movie.nfo', 'poster.jpg', 'fanart.jpg', 'metadata.json', 'job.json'):
            src = pending_dir / name
            if src.is_file():
                shutil.copy2(src, staging / name)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(final_dir))
    except Exception as exc:
        raise MetadataPackageError(f'移动到最终目录失败: {final_dir}') from exc
    else:
        shutil.rmtree(pending_dir, ignore_errors=True)

    return PackageResult(
        final_dir=final_dir,
        video_path=final_dir / video_name,
        scrape_needed=False,
        errors=[],
    )


def package_movie(mp4_path, library_root, metadata, code: str = '', page_title: str = '') -> PackageResult:
    """将 mp4 与元数据打包到 library_root/JAV/{actress}/{movie_folder}/。

    先检查最终目录不存在，绝不覆盖；不动原 mp4。
    在 mp4 同目录创建唯一 staging，cover download、create_images、视频、NFO、JSON
    全部直接在 staging 内完成，不在公共目录留下 cover/fanart/poster。
    完整生成后 shutil.move(staging, final_dir) 并确保 final_dir.parent 存在。
    视频命名 CODE.mp4（无 code 用安全原名），生成 movie.nfo、metadata.json。
    runtime 缺失用 probe_runtime_minutes；无封面或图片/下载失败只记录 errors、scrape_needed，不让视频失败。
    缺 title/actresses/genres/release_date 也标记 scrape_needed，但仍打包。
    最终目标已存在时抛 MetadataPackageError 且不动原 mp4；移动阶段冲突保留 staging 供排查。
    成功返回结果并清理空 staging 根，不残留 staging。metadata.json 用 write_metadata_json，保留 source_url/page_title/code。
    """
    import shutil

    if not isinstance(metadata, dict):
        metadata = {}
    raw_cover_file = str(metadata.get('cover_file') or '').strip()

    mp4_path = Path(mp4_path)
    library_root = Path(library_root)

    # 先规范化元数据：page_title 可作为 title 回退；列表字段均按空列表处理
    metadata = normalize_metadata(metadata, page_url=metadata.get('source_url') or '', page_title=page_title, code=code)
    code = str(metadata.get('code') or '').strip().upper()

    # 提前检查：最终目录不存在，绝不覆盖；不动原 mp4
    actress_folder, movie_folder, _ = movie_folder_parts(metadata, code=code, page_title=page_title)
    final_dir = library_root / 'JAV' / actress_folder / movie_folder
    if final_dir.exists():
        raise MetadataPackageError(f'最终目录已存在，拒绝覆盖: {final_dir}')

    # 错误与 scrape_needed 聚合（列表字段按空列表处理，不可 str([])）
    errors: List[str] = []
    scrape_needed = False

    title = str(metadata.get('title') or '').strip()
    actresses = metadata.get('actresses') or []
    genres = metadata.get('genres') or []
    release_date = str(metadata.get('release_date') or '').strip()

    if not title:
        scrape_needed = True
        errors.append('缺少字段: title')
    if not actresses:
        scrape_needed = True
        errors.append('缺少字段: actresses')
    if not genres:
        scrape_needed = True
        errors.append('缺少字段: genres')
    if not release_date:
        scrape_needed = True
        errors.append('缺少字段: release_date')

    saved_cover = _resolve_saved_cover(raw_cover_file)
    cover_url = str(metadata.get('cover_url') or '').strip()
    if not saved_cover and not cover_url:
        scrape_needed = True
        errors.append('缺少字段: cover_url')

    runtime = metadata.get('runtime')
    if runtime is None or (isinstance(runtime, str) and not runtime.strip()):
        runtime = probe_runtime_minutes(mp4_path)

    # 在 mp4 同目录创建唯一 staging，之后所有中间产物全部放在 staging 内
    staging_root = mp4_path.parent
    staging = _safe_staging_dir(staging_root)

    # 任何后续失败都保留 staging 供排查；成功则移走并清理
    try:
        # 视频移动并命名（先移到 staging，不在公共目录留原始 mp4）
        video_name = f'{code}.mp4' if code else safe_path_component(mp4_path.name, max_length=120)
        staging_video = staging / video_name
        shutil.move(str(mp4_path), str(staging_video))

        # cover download、create_images 全部在 staging 内完成
        cover_path = None
        images: dict = {}
        if saved_cover:
            try:
                cover_path = staging / 'cover_source'
                shutil.copyfile(saved_cover, cover_path)
            except Exception as exc:
                scrape_needed = True
                errors.append(f'读取本地封面失败: {exc}')
                cover_path = None
        elif cover_url:
            try:
                cover_path = download_cover(cover_url, staging)
            except Exception as exc:
                scrape_needed = True
                errors.append(f'封面下载失败: {exc}')
                cover_path = None

        if cover_path:
            try:
                images = create_images(cover_path, staging)
            except Exception as exc:
                scrape_needed = True
                errors.append(f'图片处理失败: {exc}')
            # 无论 create_images 成功或失败，都删除 staging 内的原始 cover_source，
            # 确保最终目录不保留原始封面
            try:
                if cover_path.exists():
                    cover_path.unlink()
            except OSError as exc:
                errors.append(f'删除原始封面失败: {exc}')

        # 生成 movie.nfo（直接在 staging 内）
        nfo_content = build_nfo(metadata, code=code, page_title=page_title, runtime=runtime)
        (staging / 'movie.nfo').write_text(nfo_content, encoding='utf-8')

        # metadata.json 直接在 staging 写入，保留 source_url/page_title/code
        metadata['source_url'] = str(metadata.get('source_url') or '')
        metadata['page_title'] = str(page_title or metadata.get('page_title') or '')
        metadata['code'] = code
        write_metadata_json(staging / 'metadata.json', metadata, scrape_needed=scrape_needed, errors=errors)

        # 最终目标再次确认不存在，确保 JAV/女优父目录存在，然后移动 staging
        if final_dir.exists():
            raise MetadataPackageError(f'最终目录已存在，拒绝覆盖: {final_dir}')
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(staging), str(final_dir))
        except Exception:
            # 移动阶段冲突保留 staging，不动原 mp4（已在 staging 中）
            raise MetadataPackageError(f'移动到最终目录失败: {final_dir}') from None

    except MetadataPackageError:
        raise
    except Exception as exc:
        raise MetadataPackageError(f'打包失败: {exc}') from exc
    finally:
        # 成功后 staging 已被移走不再存在；失败后保留 staging 供排查，这里只清理空的 staging 根
        try:
            if staging.exists() and not any(staging.iterdir()):
                staging.rmdir()
        except OSError:
            pass

    final_video = final_dir / video_name
    return PackageResult(
        final_dir=final_dir,
        video_path=final_video,
        scrape_needed=scrape_needed,
        errors=errors,
    )
