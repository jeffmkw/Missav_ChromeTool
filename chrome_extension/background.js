// 终端：改完后在 chrome://extensions 点「重新加载」。
// 采集：python download_missav.py --collect
// 打开影片详情页并点播放。封面由本扩展下载后 POST 到 127.0.0.1:8766/cover，
// 写入项目 covers/{番号}.jpg。采集端据此生成 pending/{番号}/ 下的 NFO 和封面。

const SERVER_BASE = "http://127.0.0.1:8766";

const VIDEO_URL_RE =
  /^https?:\/\/(?:[\w-]+\.)*missav\.ws\/(?:[a-z0-9]+\/)?(?:cn|en|ja|ko|ms|th|de|fr|vi|id|fil|pt)\/(?!actresses(?:\/|$)|playlists(?:\/|$)|genres(?:\/|$)|makers(?:\/|$)|tags(?:\/|$)|search(?:\/|$)|new(?:\/|$)|release(?:\/|$)|vip(?:\/|$)|history(?:\/|$)|contact(?:\/|$)|terms(?:\/|$))[^\s?#]+\/?$/i;

// 嗅探：UUID 后任意清晰度路径（720p / 1280x720 / …），不写死格式
const SURRIT_M3U8_RE =
  /https?:\/\/(?:[\w-]+\.)*surrit\.com\/([a-f0-9-]{36})\/([^/?#]+)\/video\.m3u8/i;

/** tabId -> 最近一次捕获 */
const byTab = new Map();
/** page_url 规范化 -> 已成功上报的签名，避免重复刷屏 */
const postedSig = new Map();

function isVideoUrl(url) {
  return typeof url === "string" && VIDEO_URL_RE.test(url);
}

function normalizeUrl(url) {
  return String(url || "").replace(/\/$/, "");
}

function parseM3u8(url) {
  const m = SURRIT_M3U8_RE.exec(url || "");
  if (!m) return null;
  return {
    video_uuid: m[1],
    quality: m[2].toLowerCase(),
    m3u8_url: url,
  };
}

/** 720p → 720；1280x720 → 用高度 720 */
function qualityRank(q) {
  const s = String(q || "").toLowerCase();
  const p = s.match(/(\d+)\s*p\b/);
  if (p) return parseInt(p[1], 10);
  const wh = s.match(/(\d+)\s*x\s*(\d+)/);
  if (wh) return parseInt(wh[2], 10);
  const n = parseInt(s.replace(/\D/g, ""), 10);
  return Number.isFinite(n) ? n : 0;
}

async function updateBadge() {
  const n = byTab.size;
  await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#2e7d32" });
}

async function uploadCover(code, coverUrl) {
  const resp = await fetch(coverUrl);
  if (!resp.ok) {
    throw new Error(`封面 HTTP ${resp.status}`);
  }
  const buf = await resp.arrayBuffer();
  if (!buf.byteLength) {
    throw new Error("封面为空");
  }
  const saved = await fetch(
    `${SERVER_BASE}/cover?code=${encodeURIComponent(code)}`,
    {
      method: "POST",
      headers: { "Content-Type": resp.headers.get("Content-Type") || "application/octet-stream" },
      body: buf,
    },
  );
  if (!saved.ok) {
    const text = await saved.text();
    throw new Error(`保存封面 HTTP ${saved.status} ${text}`);
  }
  const data = await saved.json();
  return data.cover_file || "";
}

async function postItem(item) {
  const key = normalizeUrl(item.page_url);
  const sig = `${item.video_uuid}|${item.quality || ""}|${item.m3u8_url || ""}|${JSON.stringify(item.metadata || {})}`;
  if (postedSig.get(key) === sig) return { ok: true, skipped: true };

  try {
    const resp = await fetch(`${SERVER_BASE}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [item] }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      console.warn("上报失败", resp.status, text);
      return { ok: false, error: `HTTP ${resp.status}` };
    }
    postedSig.set(key, sig);
    return { ok: true, skipped: false };
  } catch (err) {
    console.warn("上报失败（请确认已运行 --collect）", err);
    return { ok: false, error: String(err) };
  }
}

async function captureFromTab(tabId, m3u8Info) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (_) {
    return;
  }
  if (!tab?.url || !isVideoUrl(tab.url)) return;

  const prev = byTab.get(tabId);
  // 同页已有更高清晰度则保留更高的下载字段，但不提前 return：仍刷新 metadata 并上报
  const keepPrevHigh =
    prev &&
    normalizeUrl(prev.page_url) === normalizeUrl(tab.url) &&
    qualityRank(prev.quality) > qualityRank(m3u8Info.quality);

  let title = (tab.title || "").trim();
  title = title.replace(/\s*-\s*MissAV.*$/i, "").trim() || tab.url;

  const item = {
    page_url: tab.url,
    title,
    video_uuid: m3u8Info.video_uuid,
    uuids: [m3u8Info.video_uuid],
    m3u8_url: m3u8Info.m3u8_url,
    quality: m3u8Info.quality,
  };

  // 提取页面 metadata（失败不阻断 m3u8 上报）
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: extractMetadataFromPage,
    });
    const metadata = results?.[0]?.result;
    if (metadata && typeof metadata === "object") {
      item.metadata = metadata;
      if (metadata.title) {
        item.title = metadata.title;
      }
    }
  } catch (err) {
    console.warn("提取 metadata 失败，保留原流程：", err);
  }

  if (keepPrevHigh) {
    item.quality = prev.quality;
    item.m3u8_url = prev.m3u8_url;
    item.video_uuid = prev.video_uuid;
    item.uuids = prev.uuids;
  }

  const coverUrl = item.metadata?.cover_url;
  const code = item.metadata?.code;
  if (coverUrl && code) {
    try {
      const coverFile = await uploadCover(code, coverUrl);
      if (coverFile) {
        item.metadata.cover_file = coverFile;
      }
    } catch (err) {
      console.warn("封面保存失败，仍上报地址：", err);
    }
  }

  byTab.set(tabId, item);
  await updateBadge();
  await postItem(item);
}

/** 在页面上下文中执行：自包含函数，按详情行提取 metadata，不引用任何外部变量/函数 */
async function extractMetadataFromPage() {
  try {
    function cleanText(s) {
      return String(s || "").replace(/\s+/g, " ").trim();
    }

    const LABEL_KEYS = [
      ["release_date", ["发行日期", "發行日期", "発売日", "Release Date"]],
      ["code", ["番号", "品番"]],
      ["original_title", ["标题", "標題", "タイトル", "Title"]],
      ["actresses", ["女优", "女優", "Actress", "Actresses"]],
      ["male_actors", ["男优", "男優", "Actor", "Actors"]],
      ["genres", ["类型", "類型", "ジャンル", "Genre", "Genres"]],
      ["makers", ["发行商", "發行商", "メーカー", "Maker", "Studio"]],
      ["directors", ["导演", "導演", "監督", "Director"]],
      ["labels", ["标签", "標籤", "レーベル", "Label"]],
      ["series", ["系列", "シリーズ", "Series"]],
      ["runtime", ["时长", "時長", "収録時間", "Duration", "Runtime"]],
    ];

    function labelKey(text) {
      const t = cleanText(text).replace(/[:：]\s*$/, "");
      if (!t || t.length > 24) return "";
      for (const [key, names] of LABEL_KEYS) {
        if (names.some((name) => t === name)) return key;
      }
      return "";
    }

    function normalizeDate(raw) {
      const m = String(raw || "").match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
      if (!m) return "";
      return `${m[1]}-${String(m[2]).padStart(2, "0")}-${String(m[3]).padStart(2, "0")}`;
    }

    function normalizeRuntime(raw) {
      const s = cleanText(raw);
      if (!s) return "";
      const mMin = s.match(/(\d+)\s*分(?:钟)?/);
      if (mMin) return mMin[1];
      const mHms = s.match(/(\d{1,2}):(\d{2}):(\d{2})/);
      if (mHms) {
        const total = Number(mHms[1]) * 60 + Number(mHms[2]) + Math.round(Number(mHms[3]) / 60);
        return String(total);
      }
      const mNum = s.match(/^(\d{1,3})$/);
      if (mNum) return mNum[1];
      return "";
    }

    function splitList(text) {
      return cleanText(text)
        .split(/[,、，/|]/)
        .map((part) => part.trim())
        .filter(Boolean);
    }

    const hits = [];
    document.querySelectorAll("body *").forEach((el) => {
      if (el.closest("nav, header, footer")) return;
      const own = cleanText(
        Array.from(el.childNodes)
          .filter((node) => node.nodeType === 3)
          .map((node) => node.textContent)
          .join(" ")
      );
      const full = cleanText(el.textContent || "");
      const key = labelKey(own) || (full.length <= 24 ? labelKey(full) : "");
      if (!key) return;
      hits.push({ el, key });
    });
    const labels = hits.filter(
      (hit) => !hits.some((other) => other !== hit && other.key === hit.key && hit.el.contains(other.el))
    );

    let container = null;
    let bestScore = 0;
    labels.forEach((hit) => {
      let el = hit.el.parentElement;
      for (let depth = 0; depth < 8 && el && el !== document.body; depth += 1, el = el.parentElement) {
        const keys = new Set(labels.filter((item) => el.contains(item.el)).map((item) => item.key));
        if (keys.size > bestScore && (keys.has("release_date") || keys.has("code"))) {
          bestScore = keys.size;
          container = el;
        }
      }
    });

    const rows = {};
    if (container) {
      labels.forEach((hit) => {
        if (!container.contains(hit.el) || rows[hit.key]) return;
        const own = cleanText(
          Array.from(hit.el.childNodes)
            .filter((node) => node.nodeType === 3)
            .map((node) => node.textContent)
            .join(" ")
        );
        const full = cleanText(hit.el.textContent || "");
        let scope = hit.el;
        let text = full;
        if (labelKey(full) && hit.el.parentElement) {
          scope = hit.el.parentElement;
          text = cleanText(scope.textContent || "");
          if (full && text.startsWith(full)) text = cleanText(text.slice(full.length));
        } else if (own && full.startsWith(own)) {
          text = cleanText(full.slice(own.length));
        }
        text = text.replace(/^[:：\s]+/, "");
        const links = Array.from(scope.querySelectorAll("a"))
          .filter((anchor) => scope === hit.el || !hit.el.contains(anchor))
          .map((anchor) => cleanText(anchor.textContent || ""))
          .filter(Boolean);
        rows[hit.key] = { text, links };
      });
    }

    function rowList(key) {
      const row = rows[key];
      if (!row) return [];
      if (row.links.length === 1 && row.text && row.text.includes(row.links[0]) && row.text !== row.links[0]) {
        return [row.text];
      }
      if (row.links.length) {
        const seen = new Set();
        return row.links.filter((name) => {
          if (seen.has(name)) return false;
          seen.add(name);
          return true;
        });
      }
      return splitList(row.text);
    }

    function actressNames(names) {
      const seen = new Set();
      const out = [];
      names.forEach((name) => {
        const stripped = cleanText(String(name || "").replace(/\s*[(（][^()（）]*[)）]\s*/g, " "));
        stripped.split(/[,、，]/).forEach((part) => {
          const text = cleanText(part);
          if (!text || seen.has(text)) return;
          seen.add(text);
          out.push(text);
        });
      });
      return out;
    }

    const h1 = document.querySelector("h1");
    const title = cleanText(h1?.textContent || "") || cleanText(document.title || "");

    const moreLabels = /^(显示更多|顯示更多|查看更多|查看全文|Show more|もっと見る)$/;
    const firstLabel = labels.find((hit) => container && container.contains(hit.el));
    const moreButtons = Array.from(document.querySelectorAll("button, a, [role='button']")).filter((el) => {
      if (el.closest("nav, header, footer")) return false;
      const text = cleanText(el.textContent || "");
      if (!moreLabels.test(text)) return false;
      if (firstLabel && firstLabel.el.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) return false;
      return true;
    });
    moreButtons.forEach((el) => {
      try { el.click(); } catch (_) {}
    });
    if (moreButtons.length) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }

    function isPromo(el, text) {
      const promo = /电报群|電報群|无广告免费|無廣告免費|3元VPN|高峰不卡|春药商城|春藥商城|色色主播|免费漫画|免費漫畫|카지노|エロライブ|红果AI|加密货币/;
      const anchors = el.querySelectorAll ? el.querySelectorAll("a") : [];
      if (promo.test(text) && (anchors.length >= 1 || text.length < 220)) return true;
      if (anchors.length >= 5) {
        const linkLen = Array.from(anchors).reduce((sum, anchor) => sum + cleanText(anchor.textContent).length, 0);
        if (linkLen > text.length * 0.6) return true;
      }
      return false;
    }

    function plotFrom(el) {
      if (!el || el === document.body) return "";
      const text = cleanText(el.textContent || "");
      if (!text || text === title) return "";
      if (isPromo(el, text)) {
        for (const child of el.children) {
          const inner = plotFrom(child);
          if (inner) return inner;
        }
        return "";
      }
      if (text.length < 40) return "";
      if (/发行日期|發行日期|発売日|番号[:：]|品番/.test(text)) return "";
      return text.slice(0, 2000);
    }

    let plot = "";
    if (container) {
      const firstLabel = labels.find((hit) => container.contains(hit.el));
      let node = firstLabel ? firstLabel.el : container;
      while (node && node !== document.body) {
        let prev = node.previousElementSibling;
        while (prev) {
          const found = plotFrom(prev);
          if (found) {
            plot = found;
            break;
          }
          prev = prev.previousElementSibling;
        }
        if (plot) break;
        node = node.parentElement;
      }
    }
    if (!plot) {
      const metaPlot = cleanText(document.querySelector("meta[name='description']")?.getAttribute("content") || "");
      const promoText = /电报群|電報群|无广告免费|無廣告免費|3元VPN|高峰不卡|春药商城|春藥商城|色色主播|免费漫画|免費漫畫|카지노|エロライブ|红果AI|加密货币/;
      if (metaPlot.length >= 40 && !promoText.test(metaPlot)) plot = metaPlot.slice(0, 2000);
    }
    plot = plot.replace(/\s*(?:\.{3}|…)?\s*(显示更多|顯示更多|查看更多|查看全文|Show more|もっと見る)\s*$/u, "").trim();

    const ogImage = document.querySelector("meta[property='og:image']");
    let cover_url = cleanText(ogImage?.getAttribute("content") || "");
    if (!cover_url) {
      cover_url = cleanText(document.querySelector("video")?.poster || "");
    }
    if (cover_url) {
      try { cover_url = new URL(cover_url, location.href).href; } catch (_) {}
    }

    const slug = (location.pathname || "").split("/").filter(Boolean).pop() || "";
    const slugCode = slug.match(/^[A-Z]{2,10}[-]?\d{2,5}/i);
    const rowCode = cleanText(rows.code?.text || "").match(/[A-Z]{2,10}-?\d{2,5}/i);
    const code = (rowCode ? rowCode[0] : slugCode ? slugCode[0] : "").toUpperCase();

    const release_date = normalizeDate(rows.release_date?.text || "");
    const runtime = normalizeRuntime(rows.runtime?.text || "");
    const extra = {};
    if (release_date) extra.release_date = release_date;
    if (runtime) extra.runtime = runtime;

    return {
      title,
      original_title: cleanText(rows.original_title?.text || ""),
      cover_url,
      plot,
      actresses: actressNames(rowList("actresses")),
      male_actors: rowList("male_actors"),
      genres: rowList("genres"),
      makers: rowList("makers"),
      labels: rowList("labels"),
      series: rowList("series"),
      directors: rowList("directors"),
      release_date,
      runtime,
      code,
      preview_urls: [],
      extra,
    };
  } catch (err) {
    console.warn("extractMetadataFromPage error", err);
    return { title: document.title || "", cover_url: "", plot: "", preview_urls: [], extra: {} };
  }
}

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const info = parseM3u8(details.url);
    if (!info) return;
    captureFromTab(details.tabId, info).catch((err) =>
      console.warn("capture error", err)
    );
  },
  {
    urls: [
      "*://surrit.com/*/video.m3u8*",
      "*://*.surrit.com/*/video.m3u8*",
    ],
  }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  byTab.delete(tabId);
  updateBadge();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    byTab.delete(tabId);
    updateBadge();
  }
});

chrome.action.onClicked.addListener(async () => {
  // 无 popup：点击图标仅重试上报已缓存条目（不扫 DOM、不抢播放焦点逻辑之外的额外操作）
  let ok = 0;
  let fail = 0;
  for (const item of byTab.values()) {
    const r = await postItem(item);
    if (r.ok && !r.skipped) ok += 1;
    else if (!r.ok) fail += 1;
  }
  await chrome.action.setTitle({
    title:
      fail > 0
        ? `重试：成功 ${ok}，失败 ${fail}（请先运行 --collect）`
        : `已缓存 ${byTab.size} 条；播放即自动上报`,
  });
});

updateBadge();
