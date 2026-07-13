# -*- coding: utf-8 -*-
"""
终端执行指南
------------
由 download_missav.py --download-only 启动（默认开启进度页；--no-web 关闭）。

本模块提供本地下载进度页（只读）:
  http://127.0.0.1:8777
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

DEFAULT_WEB_PORT = 8777

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_MERGING = "merging"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_QUEUED: "排队中",
    STATUS_DOWNLOADING: "下载中",
    STATUS_MERGING: "合并中",
    STATUS_DONE: "已完成",
    STATUS_FAILED: "失败",
}


@dataclass
class TaskState:
    id: str
    title: str
    status: str = STATUS_QUEUED
    done: int = 0
    total: int = 0
    error: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    speed_seg_s: float | None = None
    eta_s: float | None = None
    eta_at: float | None = None
    _samples: list[tuple[float, int]] = field(default_factory=list, repr=False)

    @property
    def percent(self) -> float:
        if self.status == STATUS_DONE:
            return 100.0
        if self.status == STATUS_MERGING:
            return 99.0 if self.total > 0 else 0.0
        if self.total <= 0:
            return 0.0
        return min(100.0, round(100.0 * self.done / self.total, 1))

    def _recompute_speed_eta(self, now: float) -> None:
        """用近 30 秒分片进度估算速度与预计结束时间。"""
        self.speed_seg_s = None
        self.eta_s = None
        self.eta_at = None
        if self.status != STATUS_DOWNLOADING:
            return
        if self.total <= 0 or self.done <= 0:
            return

        cutoff = now - 30.0
        samples = [(t, d) for t, d in self._samples if t >= cutoff]
        if len(samples) < 2:
            # 样本不足时退回全程平均
            if self.started_at is not None and now > self.started_at + 1.0:
                samples = [(self.started_at, 0), (now, self.done)]
            else:
                return

        t0, d0 = samples[0]
        t1, d1 = samples[-1]
        dt = t1 - t0
        dd = d1 - d0
        if dt < 1.0 or dd <= 0:
            return

        speed = dd / dt
        self.speed_seg_s = round(speed, 2)
        remain = max(0, self.total - self.done)
        if remain <= 0:
            self.eta_s = 0.0
            self.eta_at = now
            return
        self.eta_s = round(remain / speed, 1)
        self.eta_at = now + self.eta_s

    def to_dict(self) -> dict[str, Any]:
        elapsed_s = None
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            elapsed_s = round(end - self.started_at, 1)

        speed_label = "-"
        if self.status == STATUS_DOWNLOADING and self.speed_seg_s is not None:
            speed_label = f"{self.speed_seg_s:.1f} 片/秒"
        elif self.status == STATUS_MERGING:
            speed_label = "合并中"

        eta_label = "-"
        eta_clock = "-"
        if self.status == STATUS_DOWNLOADING and self.eta_at is not None:
            eta_clock = time.strftime("%H:%M:%S", time.localtime(self.eta_at))
            if self.eta_s is not None:
                eta_label = f"预计 {eta_clock}（剩 {_fmt_remain(self.eta_s)}）"
            else:
                eta_label = f"预计 {eta_clock}"
        elif self.status == STATUS_MERGING:
            eta_label = "合并中…"
        elif self.status == STATUS_DONE:
            eta_label = "已完成"
            eta_clock = (
                time.strftime("%H:%M:%S", time.localtime(self.finished_at))
                if self.finished_at
                else "-"
            )
        elif self.status == STATUS_QUEUED:
            eta_label = "排队中"

        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "percent": self.percent,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "elapsed_s": elapsed_s,
            "speed_seg_s": self.speed_seg_s,
            "speed_label": speed_label,
            "eta_s": self.eta_s,
            "eta_at": self.eta_at,
            "eta_clock": eta_clock,
            "eta_label": eta_label,
        }


def _fmt_remain(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}小时{m}分"
    if m > 0:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


class ProgressStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskState] = {}
        self._order: list[str] = []
        self.started_at = time.time()
        self.output_dir = ""
        self.parallel = 0
        self.workers = 0

    def reset(
        self,
        items: list[tuple[str, str]],
        *,
        output_dir: str = "",
        parallel: int = 0,
        workers: int = 0,
    ) -> None:
        with self._lock:
            self._tasks.clear()
            self._order.clear()
            self.started_at = time.time()
            self.output_dir = output_dir
            self.parallel = parallel
            self.workers = workers
            now = time.time()
            for task_id, title in items:
                self._tasks[task_id] = TaskState(
                    id=task_id, title=title, updated_at=now
                )
                self._order.append(task_id)

    def set_phase(self, task_id: str, phase: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            now = time.time()
            if phase == "segments":
                task.status = STATUS_DOWNLOADING
                if task.started_at is None:
                    task.started_at = now
                if not task._samples:
                    task._samples.append((now, task.done))
            elif phase == "merge":
                task.status = STATUS_MERGING
                if task.started_at is None:
                    task.started_at = now
                task.speed_seg_s = None
                task.eta_s = None
                task.eta_at = None
            elif phase == "probe" or phase == "parse":
                task.status = STATUS_DOWNLOADING
                if task.started_at is None:
                    task.started_at = now
            task.updated_at = now

    def set_progress(self, task_id: str, done: int, total: int) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            now = time.time()
            task.status = STATUS_DOWNLOADING
            task.done = done
            task.total = total
            if task.started_at is None:
                task.started_at = now
            if not task._samples or task._samples[-1][1] != done:
                task._samples.append((now, done))
            # 只保留近 60 秒样本，避免列表无限增长
            cutoff = now - 60.0
            task._samples = [(t, d) for t, d in task._samples if t >= cutoff]
            task._recompute_speed_eta(now)
            task.updated_at = now

    def mark_done(self, task_id: str, done: int | None = None, total: int | None = None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if done is not None:
                task.done = done
            if total is not None:
                task.total = total
            if task.total <= 0 and task.done > 0:
                task.total = task.done
            task.status = STATUS_DONE
            task.speed_seg_s = None
            task.eta_s = None
            task.eta_at = None
            task.finished_at = time.time()
            task.updated_at = task.finished_at

    def mark_failed(self, task_id: str, error: str = "") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.status = STATUS_FAILED
            task.error = (error or "")[:300]
            task.speed_seg_s = None
            task.eta_s = None
            task.eta_at = None
            task.finished_at = time.time()
            task.updated_at = task.finished_at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # 刷新下载中任务的 ETA（即使进度短暂未更新）
            now = time.time()
            for task in self._tasks.values():
                if task.status == STATUS_DOWNLOADING:
                    task._recompute_speed_eta(now)
            tasks = [self._tasks[i].to_dict() for i in self._order if i in self._tasks]
            counts = {
                STATUS_QUEUED: 0,
                STATUS_DOWNLOADING: 0,
                STATUS_MERGING: 0,
                STATUS_DONE: 0,
                STATUS_FAILED: 0,
            }
            for t in tasks:
                counts[t["status"]] = counts.get(t["status"], 0) + 1
            return {
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 1),
                "output_dir": self.output_dir,
                "parallel": self.parallel,
                "workers": self.workers,
                "total": len(tasks),
                "counts": counts,
                "tasks": tasks,
            }


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>下载任务</title>
  <style>
    :root {
      --bg: #1a1d21;
      --panel: #22262b;
      --row: #2a2f36;
      --row-hover: #323842;
      --border: #3a414b;
      --text: #e8eaed;
      --muted: #9aa0a6;
      --accent: #3d8bfd;
      --ok: #3dd68c;
      --warn: #f5a524;
      --err: #f31260;
      --bar-bg: #14171b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    header {
      padding: 16px 20px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 18px;
      font-weight: 600;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 12px;
      word-break: break-all;
    }
    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .chip {
      background: var(--row);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 12px;
    }
    .chip b { color: var(--text); font-weight: 600; }
    .chip.down b { color: var(--accent); }
    .chip.merge b { color: var(--warn); }
    .chip.done b { color: var(--ok); }
    .chip.fail b { color: var(--err); }
    main { padding: 12px 16px 32px; }
    .task {
      background: var(--row);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 8px;
    }
    .task:hover { background: var(--row-hover); }
    .top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 8px;
    }
    .title {
      font-size: 14px;
      line-height: 1.4;
      flex: 1;
      word-break: break-word;
    }
    .badge {
      flex-shrink: 0;
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 3px;
      border: 1px solid var(--border);
      color: var(--muted);
    }
    .badge.downloading { color: var(--accent); border-color: #2a5a9e; }
    .badge.merging { color: var(--warn); border-color: #8a5a12; }
    .badge.done { color: var(--ok); border-color: #1f6b45; }
    .badge.failed { color: var(--err); border-color: #8a1f3d; }
    .bar {
      height: 8px;
      background: var(--bar-bg);
      border-radius: 4px;
      overflow: hidden;
      margin-bottom: 8px;
    }
    .bar > i {
      display: block;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #2f6fed, #5aa2ff);
      transition: width 0.35s ease;
    }
    .bar.done > i { background: var(--ok); }
    .bar.failed > i { background: var(--err); }
    .bar.merging > i { background: var(--warn); }
    .foot {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 12px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .metrics {
      display: flex;
      flex-wrap: wrap;
      gap: 10px 14px;
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }
    .metrics b {
      color: var(--text);
      font-weight: 600;
    }
    .err {
      margin-top: 6px;
      color: var(--err);
      font-size: 12px;
      word-break: break-word;
    }
    .empty {
      text-align: center;
      color: var(--muted);
      padding: 48px 12px;
    }
  </style>
</head>
<body>
  <header>
    <h1>下载任务</h1>
    <div class="meta" id="meta">加载中…</div>
    <div class="stats" id="stats"></div>
  </header>
  <main id="list"><div class="empty">暂无任务</div></main>
  <script>
    function fmtElapsed(s) {
      if (s == null) return "-";
      s = Math.floor(s);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) return h + ":" + String(m).padStart(2,"0") + ":" + String(sec).padStart(2,"0");
      return m + ":" + String(sec).padStart(2,"0");
    }
    function render(data) {
      const c = data.counts || {};
      document.getElementById("meta").textContent =
        "输出目录: " + (data.output_dir || "-") +
        "  |  并行 " + (data.parallel || 0) +
        " / 分片线程 " + (data.workers || 0) +
        "  |  运行 " + fmtElapsed(data.uptime_s);
      document.getElementById("stats").innerHTML = [
        ['全部', data.total || 0, ''],
        ['排队', c.queued || 0, ''],
        ['下载中', c.downloading || 0, 'down'],
        ['合并中', c.merging || 0, 'merge'],
        ['已完成', c.done || 0, 'done'],
        ['失败', c.failed || 0, 'fail'],
      ].map(([k,v,cls]) => '<div class="chip ' + cls + '">' + k + ' <b>' + v + '</b></div>').join("");

      const list = document.getElementById("list");
      const tasks = data.tasks || [];
      if (!tasks.length) {
        list.innerHTML = '<div class="empty">暂无任务</div>';
        return;
      }
      // 进行中靠前，完成/失败靠后
      const rank = {downloading:0, merging:1, queued:2, failed:3, done:4};
      tasks.sort((a,b) => (rank[a.status]??9) - (rank[b.status]??9));
      list.innerHTML = tasks.map(t => {
        const pct = t.percent || 0;
        const segs = (t.total > 0)
          ? (t.done + " / " + t.total + " 分片")
          : (t.status === "merging" ? "合并 mp4…" : "等待中");
        return (
          '<div class="task">' +
            '<div class="top">' +
              '<div class="title">' + escapeHtml(t.title || t.id) + '</div>' +
              '<span class="badge ' + t.status + '">' + escapeHtml(t.status_label || t.status) + '</span>' +
            '</div>' +
            '<div class="bar ' + t.status + '"><i style="width:' + pct + '%"></i></div>' +
            '<div class="foot">' +
              '<span>' + segs + '</span>' +
              '<span>' + pct.toFixed(1) + '% · ' + fmtElapsed(t.elapsed_s) + '</span>' +
            '</div>' +
            '<div class="metrics">' +
              '<span>速度 <b>' + escapeHtml(t.speed_label || '-') + '</b></span>' +
              '<span>预计结束 <b>' + escapeHtml(t.eta_label || '-') + '</b></span>' +
            '</div>' +
            (t.error ? '<div class="err">' + escapeHtml(t.error) + '</div>' : '') +
          '</div>'
        );
      }).join("");
    }
    function escapeHtml(s) {
      return String(s)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
    }
    async function tick() {
      try {
        const res = await fetch("/api/progress?_=" + Date.now());
        if (!res.ok) throw new Error("HTTP " + res.status);
        render(await res.json());
      } catch (e) {
        document.getElementById("meta").textContent = "无法连接进度服务: " + e.message;
      }
    }
    tick();
    setInterval(tick, 1000);
  </script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    store: ProgressStore

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/progress":
            payload = json.dumps(self.store.snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


class ProgressWebServer:
    def __init__(self, store: ProgressStore, host: str = "127.0.0.1", port: int = DEFAULT_WEB_PORT):
        self.store = store
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        last_err: Exception | None = None
        for port in range(self.port, self.port + 20):
            try:
                handler = type("Handler", (_Handler,), {"store": self.store})
                httpd = ThreadingHTTPServer((self.host, port), handler)
                self.port = port
                self._httpd = httpd
                self._thread = threading.Thread(
                    target=httpd.serve_forever,
                    name="progress-web",
                    daemon=True,
                )
                self._thread.start()
                return self.url
            except OSError as exc:
                last_err = exc
                continue
        raise RuntimeError(f"无法绑定进度页端口（从 {self.port} 起）: {last_err}")

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
