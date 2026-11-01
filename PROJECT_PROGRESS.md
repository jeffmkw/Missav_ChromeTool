# 项目进展备注

更新时间：2026-11-01

## 当前目标

采集时按 MissAV 页面把 NFO 和封面定稿，下载只负责拉视频、合并，再搬进片库。不再使用 javsp。

## 已完成

- 采集资料齐全时写入项目 `pending/{番号}/`：`movie.nfo`、`poster.jpg`、`fanart.jpg`、`metadata.json`、`job.json`，清单标 `ready`。
- 缺番号、标题、女优、类型、发行日期、简介、发行商、封面中任何一项，标 `incomplete`，不下载。
- 下载（本机或 NAS）把视频下到 `.env` 里的硬盘目录，合并后补时长，再搬到 `{片库}/JAV/{女优}/[番号] 标题/`。
- 扩展 `4.3.3`：
  - 按详情行采集标题、日文标题、女优、男优、类型、发行商、导演、标签、系列、日期和简介。
  - 简介跳过广告条；有「显示更多」时先点开再读。
  - 女优名去掉括号中的别名。
- 路径只在 `.env` 配置：`DOWNLOAD_DIR`、`JAV_LIBRARY_DIR`、`NAS_DOWNLOAD_DIR`、`NAS_JAV_LIBRARY_DIR`。`pending/` 固定在项目目录内。
- 已用真实页面核对：XVSR-908 已下载入库；XVSR-823 已写入 `pending` 且为 `ready`；HUNBL-021 简介正确，但女优为空，未生成 `pending`。

## 主要文件

- `chrome_extension/background.js`、`chrome_extension/manifest.json`：页面采集。
- `missav_tab_check.py`：上报、清单、`pending/`。
- `missav_metadata.py`：NFO、封面、女优名清理、入库搬运。
- `download_missav.py`、`download_missav_nas.py`：本机与 NAS 下载。
- `CHANGELOG.md`、`.env.example`。

## 仍待处理

- 有的片子页面上有女优，采集结果仍是空，例如 HUNBL-021。缺女优就不会进 `pending`。
- 标签行有时没写入，例如 XVSR-823 网页上的 `MAX-A`。
- 地址里的 `uncensored-leak` 会被去掉，番号保存为基础番号，例如 `XVSR-823-UNCENSORED-LEAK` 记成 `XVSR-823`。
- 片库里已经有的片子，重新采集只刷新 `pending/`，不会改片库里的旧 NFO。
