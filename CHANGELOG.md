# Changelog

本文件记录项目的重要变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed
- **采集定稿后再下载（2026-11-01）**
  - **修改原因**：下载完成后再拼 NFO，和网页上的资料对不齐；也不再把成片交给 javsp 刮削
  - 采集时资料齐全（番号、标题、女优、类型、发行日期、简介、发行商、封面）才写入项目 `pending/{番号}/`：`movie.nfo`、`poster.jpg`、`fanart.jpg`、`metadata.json`、`job.json`，清单标 `ready`
  - 缺任何一项标 `incomplete`，不下载。导演、男优、标签、日文标题、系列有则写入
  - 下载只负责分片、合并、用成片补时长，再把该目录搬到 `{JAV_LIBRARY_DIR}/JAV/{女优}/[番号] 标题/`。本机与 NAS 两条下载都如此，不再送入 `_incoming`
  - 视频缓存在 `.env` 的硬盘目录（`DOWNLOAD_DIR` / `NAS_DOWNLOAD_DIR`）。刮削文件留在项目 `pending/`，清空下载缓存不会丢掉已采资料
  - 扩展 `4.3.3`：按详情行采集；简介跳过广告条，有「显示更多」时先点开再读；女优名去掉括号中的别名
  - 扩展更新后需在 `chrome://extensions` 点「重新加载」

### Added
- **MissAV 元数据与入库模式（2026-10-27）**
  - 采集扩展将标题/演员/类别/日期/封面等元数据写入 `check_list2.json`；扩展代码更新后需在 `chrome://extensions` 点「重新加载」
  - 下载后若条目带实质元数据（封面/演员/类别/日期/片商/系列/简介等任一），直接生成 `JAV_LIBRARY_DIR/JAV/{女优}/[番号] 标题/{番号}.mp4`、`movie.nfo`、`poster.jpg`、`fanart.jpg`、`metadata.json`
  - 封面右侧裁剪：横版按宽度 × 0.528 裁右侧写 `poster.jpg`，完整图写 `fanart.jpg`；无 JavSP 也可用
  - `metadata.json` 含 `source`、`scrape_needed`、`errors` 与规范化元数据；`scrape_needed=true`/`errors` 表示建议补充刮削，**不算视频下载失败**
  - 新增依赖 `Pillow>=10.0`（保留 httpx/rich）

### Changed
- **入库流程对接 javsp 刮削（2026-10-21）**
  - **修改原因**：① 成片整理 workflow 已从 Windows 本机迁移到飞牛 NAS（下载、入库、刮削全在 NAS 完成）；② 为兼容 NAS 上 javsp 刮削器的工作流程——javsp 递归扫描片库后按 `JAV/{女优}/[番号] 标题/` 重组归档，片库内再按番号前缀分文件夹（`JAV/{PREFIX}/`）已无意义且会被刮削移空
  - **新优先级**：MissAV 元数据为主入库路径（带实质元数据直达 `JAV/{女优}/[番号] 标题/`，生成 NFO/封面/JSON），JavSP 刮削仅作**可选兜底**处理旧清单/仅标题条目，移入 `_incoming/`
  - 具体变更：片库内不再按番号前缀分文件夹，识别出番号的成片统一平铺到 `JAV/_incoming/`，交由 NAS 上的 javsp 容器刮削后自动归档
  - `missav_tab_check.py`：`move_mp4_to_jav_library()` 入库目标改为 `{jav_root}/_incoming/`；`extract_code_prefix()` 保留仅作展示/日志用途
  - `organize_jav_files.py`：入库目标由 `{PREFIX}` 改为 `_incoming`（新增 `INCOMING_FOLDER` 常量）
  - 无法识别番号的文件仍进 `_UNKNOWN/`（javsp 刮削时也会把识别失败的影片自动移至片库 `_UNKNOWN/`，javsp 侧已配置忽略该目录避免重复扫描）

### Notes
- 日常流程：`--collect` 写入 `pending/{番号}/` → `--download-only` 下载并搬入片库 `JAV/{女优}/[番号] 标题/`
- 路径只在 `.env` 配置：`DOWNLOAD_DIR`、`JAV_LIBRARY_DIR`、`NAS_DOWNLOAD_DIR`、`NAS_JAV_LIBRARY_DIR`。`pending/` 固定在项目目录内
- 去重扫描：`--sync-downloaded` 指向片库（如 `JAV_LIBRARY_DIR` 或 `NAS_JAV_LIBRARY_DIR`）

## [2026-07-18]

### Added
- 新增本地片库名单 `downloaded_jav.txt`：可从本地目录扫描已有片子（`--sync-downloaded`）
- 采集时，本地已有的番号会自动跳过；下载成功后会把番号记进名单
- 采集时终端即时打印每条上报结果（新增、更新、本地已有、清单已有、清晰度忽略）

### Changed
- 下载兜底探测也同时支持这两种清晰度写法
- 默认下载速度提升：同时下 4 部，每部 20 个分片线程依然不会被反爬

### Fixed
- Chrome 扩展升级到 `4.1.0`：能识别更多清晰度路径（如 `720p`、`1280x720`），不再只认一种写法