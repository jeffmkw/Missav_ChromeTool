const SERVER_BASE = "http://127.0.0.1:8766";

const VIDEO_URL_RE =
  /^https?:\/\/(?:[\w-]+\.)*missav\.ws\/(?:[a-z0-9]+\/)?(?:cn|en|ja|ko|ms|th|de|fr|vi|id|fil|pt)\/(?!actresses(?:\/|$)|playlists(?:\/|$)|genres(?:\/|$)|makers(?:\/|$)|tags(?:\/|$)|search(?:\/|$)|new(?:\/|$)|release(?:\/|$)|vip(?:\/|$)|history(?:\/|$)|contact(?:\/|$)|terms(?:\/|$))[^\s?#]+\/?$/i;

const SURRIT_M3U8_RE =
  /https?:\/\/(?:[\w-]+\.)*surrit\.com\/([a-f0-9-]{36})\/([0-9]+p)\/video\.m3u8/i;

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

function qualityRank(q) {
  const n = parseInt(String(q || "").replace(/\D/g, ""), 10);
  return Number.isFinite(n) ? n : 0;
}

async function updateBadge() {
  const n = byTab.size;
  await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#2e7d32" });
}

async function postItem(item) {
  const key = normalizeUrl(item.page_url);
  const sig = `${item.video_uuid}|${item.quality || ""}|${item.m3u8_url || ""}`;
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
  // 同页已有更高清晰度则保留更高的；同级则用最新
  if (
    prev &&
    normalizeUrl(prev.page_url) === normalizeUrl(tab.url) &&
    qualityRank(prev.quality) > qualityRank(m3u8Info.quality)
  ) {
    return;
  }

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
  byTab.set(tabId, item);
  await updateBadge();
  await postItem(item);
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
