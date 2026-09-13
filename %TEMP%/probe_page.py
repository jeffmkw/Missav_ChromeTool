# -*- coding: utf-8 -*-
from pathlib import Path
import os
import re
import subprocess

url = "https://missav.ws/dm81/cn/ssis-566-uncensored-leak"
out = Path(os.environ["TEMP"]) / "missav_ssis566.html"
cookie = Path(r"v:\download_from_missav\missav.ws_cookies.txt")
ua = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
cmd = [
    "curl.exe",
    "-sL",
    "-A",
    ua,
    "-b",
    str(cookie),
    "-e",
    "https://missav.ws/",
    url,
    "-o",
    str(out),
    "-w",
    "http_code=%{http_code} size=%{size_download} final=%{url_effective}\n",
]
print(subprocess.run(cmd, capture_output=True, text=True).stdout)
raw = out.read_text(encoding="utf-8", errors="replace")
print("len", len(raw))
m = re.search(r"<title>(.*?)</title>", raw, re.I | re.S)
print("title", (m.group(1).strip()[:120] if m else None))
for pat in [
    "surrit",
    "m3u8",
    "Just a moment",
    "cf-browser-verification",
    "playlist",
    "video.m3u8",
    "eval(function(p,a,c,k,e",
]:
    print(pat, raw.count(pat))

urls = re.findall(r"https?://[^\s\"'<>]+", raw)
interesting = [
    u
    for u in urls
    if any(x in u.lower() for x in ["surrit", "m3u8", "cloudfront", "cdn", "bunny"])
]
print("interesting", len(interesting))
for u in interesting[:50]:
    print(u[:240])

uuids = list(
    re.finditer(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
        raw,
        re.I,
    )
)
print("uuid_count", len(uuids))
for m in uuids[:5]:
    start = max(0, m.start() - 100)
    end = min(len(raw), m.end() + 100)
    print("CTX:", re.sub(r"\s+", " ", raw[start:end])[:260])
