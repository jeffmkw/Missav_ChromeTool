# Changelog

本文件记录项目的重要变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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