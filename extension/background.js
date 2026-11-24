// Projecter browser extension: background service worker.
// Watches the streams each tab's video player loads, and sends them to the Projecter app
// on this laptop (http://localhost:8765), which plays them on your TVs.

const API = 'http://localhost:8765';
const HOST = 'com.projecter.launcher';          // native helper that starts Projecter
const STREAM_RE = /\.(m3u8|mpd)(\?|#|$)|\.(mp4|m4v|webm|mov)(\?|#|$)|mpegurl/i;
const SKIP_RE = /googlevideo\.com|doubleclick|googlesyndication|imasdk|\/ads?[\/_-]|\/vast|\.(ts|m4s|aac|vtt|srt|jpg|png)(\?|$)/i;
const KEEP_HEADERS = {referer: 'Referer', origin: 'Origin', 'user-agent': 'User-Agent', cookie: 'Cookie', authorization: 'Authorization'};
const DEFAULTS = {resume: true, pauseBrowser: true, overlay: true, autoRemote: true, autoSubs: true};
const SUB_RE = /\.(vtt|srt|ass|ssa)(\?|#|$)/i;
const SUB_SKIP = /thumb|sprite|storyboard|preview|chapters|seek/i;
const SUB_TYPES = /text\/vtt|x-subrip|text\/srt|x-ssa|x-ass/i;

// ------------------------------------------------------------------ stream sniffing
const streams = {};   // tabId -> [{url, kind, headers, time}] newest first
const subs = {};      // tabId -> [{url, headers, label, time}] subtitle files the page loaded
const tabHosts = {};
let restored = null;
const restore = () => restored || (restored = chrome.storage.session.get(['streams', 'subs']).then(s => {
  Object.assign(streams, s.streams || {}); Object.assign(subs, s.subs || {});
}));
let saveTimer = null;
const save = () => { clearTimeout(saveTimer); saveTimer = setTimeout(() => chrome.storage.session.set({streams, subs}), 400); };

function pickHeaders(list) {
  const headers = {};
  for (const h of list || []) {
    const k = KEEP_HEADERS[h.name.toLowerCase()];
    if (k) headers[k] = h.value;
  }
  return headers;
}

function addSub(tabId, url, headers) {
  if (SUB_SKIP.test(url) || url.startsWith(API)) return;
  const list = subs[tabId] || (subs[tabId] = []);
  const i = list.findIndex(s => s.url === url);
  if (i >= 0) { Object.assign(list[i].headers, headers); return; }
  // Many small .vtt files from one folder = pieces of a streamed subtitle track, not whole files
  const dir = url.split('?')[0].replace(/[^/]*$/, '');
  if (list.filter(s => s.url.split('?')[0].startsWith(dir)).length >= 3) {
    subs[tabId] = list.filter(s => !s.url.split('?')[0].startsWith(dir));
    return;
  }
  const name = decodeURIComponent(url.split('?')[0].split('/').pop() || '');
  list.unshift({url, headers, time: Date.now(), label: /\.(vtt|srt|ass|ssa)$/i.test(name) && name.length < 40 ? name : 'Subtitles from the page'});
  list.length = Math.min(list.length, 12);
  save();
}

chrome.webRequest.onSendHeaders.addListener(async d => {
  if (d.tabId < 0) return;
  if (SUB_RE.test(d.url)) { await restore(); addSub(d.tabId, d.url, pickHeaders(d.requestHeaders)); return; }
  if (!STREAM_RE.test(d.url) || SKIP_RE.test(d.url) || d.url.startsWith(API)) return;
  await restore();
  const headers = pickHeaders(d.requestHeaders);
  const kind = /\.m3u8|mpegurl/i.test(d.url) ? 'hls' : /\.mpd/i.test(d.url) ? 'dash' : 'file';
  const list = streams[d.tabId] || (streams[d.tabId] = []);
  const i = list.findIndex(s => s.url === d.url);
  if (i >= 0) list.splice(i, 1);
  list.unshift({url: d.url, kind, headers, time: Date.now(), from: d.initiator || ''});
  list.length = Math.min(list.length, 30);
  save();
  badge(d.tabId);
}, {urls: ['<all_urls>'], types: ['xmlhttprequest', 'media', 'other']}, ['requestHeaders', 'extraHeaders']);

// Subtitles served without a file extension (e.g. /api/subtitle?id=…): recognise them by type.
chrome.webRequest.onHeadersReceived.addListener(async d => {
  if (d.tabId < 0 || SUB_RE.test(d.url)) return;
  const type = (d.responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type');
  if (type && SUB_TYPES.test(type.value)) { await restore(); addSub(d.tabId, d.url, {}); }
}, {urls: ['<all_urls>'], types: ['xmlhttprequest', 'other']}, ['responseHeaders']);

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (!info.url) return;
  await restore();
  let host = '';
  try { host = new URL(info.url).host; } catch (e) {}
  if (tabHosts[tabId] && tabHosts[tabId] !== host) { delete streams[tabId]; delete subs[tabId]; save(); badge(tabId); }
  tabHosts[tabId] = host;
});
chrome.tabs.onRemoved.addListener(tabId => { delete streams[tabId]; delete subs[tabId]; delete tabHosts[tabId]; save(); });

function badge(tabId) {
  const n = (streams[tabId] || []).length;
  chrome.action.setBadgeText({tabId, text: n ? '●' : ''}).catch(() => {});
  chrome.action.setBadgeBackgroundColor({tabId, color: '#7a5cff'}).catch(() => {});
}

// ------------------------------------------------------------------ talking to Projecter
async function api(path, body, timeout = 120000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(API + path, body === undefined ? {signal: ctl.signal} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: ctl.signal});
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    return j;
  } finally { clearTimeout(t); }
}

async function getState() {
  try { const s = await api('/api/state', undefined, 2500); refreshMenus(s.devices); return s; }
  catch (e) { return null; }
}

async function startServer() {
  let s = await getState();
  if (s) return s;
  try {
    await chrome.runtime.sendNativeMessage(HOST, {cmd: 'start'});
  } catch (e) {
    throw new Error("Projecter isn't running, and the extension couldn't start it. Double-click Projecter.bat on your laptop.");
  }
  for (let i = 0; i < 30; i++) {          // the TVs need a few seconds to be found
    s = await getState();
    if (s && s.devices.some(d => d.online)) return s;
    await new Promise(r => setTimeout(r, 500));
  }
  if (s) return s;
  throw new Error('Projecter is starting but not answering yet. Try again in a moment.');
}

const settings = async () => Object.assign({}, DEFAULTS, await chrome.storage.sync.get(Object.keys(DEFAULTS)));

// ------------------------------------------------------------------ what's on the page
async function pageInfo(tabId) {
  let frames = [];
  try {
    const res = await chrome.scripting.executeScript({target: {tabId, allFrames: true}, func: () => {
      const vids = [...document.querySelectorAll('video')].map(v => {
        const r = v.getBoundingClientRect();
        return {src: v.currentSrc || v.src || '', time: v.currentTime || 0,
                duration: isFinite(v.duration) ? v.duration : 0, paused: v.paused,
                area: Math.round(r.width * r.height), poster: v.poster || ''};
      }).filter(v => v.area > 2000 || v.duration > 0);
      // Subtitle tracks the player gave the browser (incl. ones added with its "add subtitles"
      // button). Tracks without a file (added by script) are sent as their text.
      const tracks = [];
      for (const v of document.querySelectorAll('video')) {
        const els = [...v.querySelectorAll('track')];
        for (const t of v.textTracks) {
          if (t.kind !== 'subtitles' && t.kind !== 'captions') continue;
          const el = els.find(e => e.track === t);
          const src = el && /^https?:/i.test(el.src) ? el.src : '';
          const cues = !src && t.cues && t.cues.length
            ? [...t.cues].slice(0, 6000).map(c => [c.startTime, c.endTime, c.text || '']) : null;
          if (src || cues) tracks.push({label: t.label, lang: t.language, mode: t.mode, src, cues});
        }
      }
      const og = document.querySelector('meta[property="og:image"],meta[name="twitter:image"]');
      return {frame: location.href, top: window === window.top, title: document.title,
              image: og ? og.content : '', vids, tracks};
    }});
    frames = res.map(r => r.result).filter(Boolean);
  } catch (e) { /* chrome:// pages, the web store, etc. */ }
  const tracks = frames.flatMap(f => (f.tracks || []).map(t => Object.assign({frame: f.frame}, t)));
  const vids = frames.flatMap(f => f.vids.map(v => Object.assign({frame: f.frame}, v)));
  // the main video: playing beats paused, then the biggest
  vids.sort((a, b) => (a.paused - b.paused) || (b.area - a.area));
  const top = frames.find(f => f.top) || {};
  await restore();
  return {video: vids[0] || null, videoCount: vids.length, image: top.image || '', tracks,
          subs: (subs[tabId] || []).length + tracks.length,
          streams: (streams[tabId] || []).map(s => ({kind: s.kind, url: s.url}))};
}

const vttTime = t => { const ms = Math.max(0, Math.round(t * 1000));
  return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`; };
const toVTT = cues => 'WEBVTT\n\n' + cues.map(([a, b, t]) => `${vttTime(a)} --> ${vttTime(b)}\n${t}`).join('\n\n') + '\n';

// Everything subtitle-like on the page, with the one the page is showing marked active.
async function collectSubtitles(tabId, info, pageUrl, auto) {
  const out = [];
  for (const t of info.tracks) {
    const label = t.label || t.lang || 'Subtitles';
    if (t.src) out.push({label, lang: t.lang, url: t.src, headers: await withHeaders({url: t.src}, t.frame), active: t.mode === 'showing'});
    else out.push({label, lang: t.lang, text: toVTT(t.cues), active: t.mode === 'showing'});
  }
  const net = subs[tabId] || [];
  for (const s of net) {
    if (out.some(x => x.url === s.url)) continue;
    out.push({label: s.label, url: s.url, headers: await withHeaders(s, pageUrl), active: false});
  }
  // Players that draw subtitles themselves only download the file you picked: if the page
  // fetched one and nothing else is marked as showing, that's the one on screen.
  if (!out.some(s => s.active) && net.length) {
    const s = out.find(x => x.url === net[0].url);
    if (s) s.active = true;
  }
  if (!auto) out.forEach(s => { s.active = false; });
  return out.slice(0, 20);
}

async function cookieHeader(url) {
  try {
    const cs = await chrome.cookies.getAll({url});
    return cs.map(c => `${c.name}=${c.value}`).join('; ');
  } catch (e) { return ''; }
}

async function withHeaders(s, referer) {
  const h = Object.assign({}, s.headers || {});
  if (!h.Cookie) { const c = await cookieHeader(s.url); if (c) h.Cookie = c; }
  if (!h['User-Agent']) h['User-Agent'] = navigator.userAgent;
  if (!h.Referer && referer) h.Referer = referer;
  return h;
}

async function cast(tabId, device, opts = {}) {
  const st = await startServer();
  const dev = st.devices.find(d => d.id === device);
  if (!dev) throw new Error('That TV is no longer available.');
  const tab = await chrome.tabs.get(tabId);
  const info = await pageInfo(tabId);
  const video = opts.video || info.video;
  const list = (streams[tabId] || []).slice();
  const candidates = [];
  const direct = opts.srcUrl || (video && video.src);
  if (direct && /^https?:/i.test(direct)) {
    const seen = list.find(s => s.url === direct);
    candidates.push({url: direct, headers: await withHeaders(seen || {url: direct}, (video && video.frame) || tab.url)});
  }
  const order = {hls: 0, dash: 1, file: 2};
  list.sort((a, b) => (order[a.kind] - order[b.kind]) || (b.time - a.time));
  for (const s of list) {
    if (candidates.some(c => c.url === s.url)) continue;
    candidates.push({url: s.url, headers: await withHeaders(s, tab.url)});
  }
  const set = await settings();
  const {subStyle} = await chrome.storage.sync.get('subStyle');
  const payload = {
    device, url: opts.pageUrl || tab.url, candidates: opts.pageUrl ? [] : candidates,
    title: opts.pageUrl ? '' : tab.title, thumb: (video && video.poster) || info.image || '',
    start: set.resume && video && !opts.pageUrl ? video.time : 0,
    subtitles: opts.pageUrl ? [] : await collectSubtitles(tabId, info, tab.url, set.autoSubs),
    sub_style: subStyle || undefined,
  };
  const r = await api('/api/cast', payload);
  chrome.storage.local.set({lastDevice: device});
  if (set.autoRemote && !opts.fromPopup) openRemote(false).catch(() => {});
  if (set.pauseBrowser && !opts.pageUrl) {
    chrome.scripting.executeScript({target: {tabId, allFrames: true},
      func: () => document.querySelectorAll('video').forEach(v => v.pause())}).catch(() => {});
  }
  return Object.assign({device: dev.name}, r);
}

// ------------------------------------------------------------------ pop-out remote window
async function openRemote(focusExisting = true, hash = '') {
  const {remoteWin} = await chrome.storage.session.get('remoteWin');
  if (remoteWin) {
    try {
      const w = await chrome.windows.get(remoteWin, {populate: true});
      if (hash && w.tabs && w.tabs[0]) await chrome.tabs.update(w.tabs[0].id, {url: chrome.runtime.getURL('remote.html' + hash)});
      if (focusExisting || w.state === 'minimized') await chrome.windows.update(remoteWin, {focused: true, state: 'normal'});
      return;
    } catch (e) { /* it was closed */ }
  }
  // place it at the top-right of the current browser window
  let left, top;
  try {
    const cur = await chrome.windows.getLastFocused();
    if (cur && cur.type === 'normal') { left = cur.left + cur.width - 400; top = cur.top + 90; }
  } catch (e) {}
  const w = await chrome.windows.create({url: chrome.runtime.getURL('remote.html' + hash), type: 'popup',
                                         width: 390, height: 640, left, top, focused: true});
  await chrome.storage.session.set({remoteWin: w.id});
}
chrome.windows.onRemoved.addListener(async id => {
  const {remoteWin} = await chrome.storage.session.get('remoteWin');
  if (id === remoteWin) chrome.storage.session.remove('remoteWin');
});

function toast(tabId, text, ok) {
  chrome.tabs.sendMessage(tabId, {type: 'toast', text, ok}).catch(() => {});
}

// ------------------------------------------------------------------ right-click menu
let menuChain = Promise.resolve();
let menuSig = '';
function refreshMenus(devices = []) {
  const sig = JSON.stringify(devices.map(d => [d.id, d.name, d.status]));
  if (sig === menuSig) return;
  menuSig = sig;
  const contexts = ['page', 'video', 'link', 'frame'];
  menuChain = menuChain.then(async () => {
    await chrome.contextMenus.removeAll();
    chrome.contextMenus.create({id: 'root', title: 'Project to TV', contexts});
    if (!devices.length) {
      chrome.contextMenus.create({id: 'start', parentId: 'root', title: 'Start Projecter', contexts});
      return;
    }
    for (const d of devices) {
      chrome.contextMenus.create({id: 'dev:' + d.id, parentId: 'root', contexts,
        title: d.name + (d.status === 'offline' ? '  (off)' : d.status === 'standby' ? '  (standby)' : ''),
        enabled: d.status !== 'offline'});
    }
  }).catch(() => {});
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || tab.id < 0) return;
  if (info.menuItemId === 'start') {
    try { await startServer(); toast(tab.id, 'Projecter is running', true); }
    catch (e) { toast(tab.id, e.message, false); }
    return;
  }
  if (!String(info.menuItemId).startsWith('dev:')) return;
  const device = String(info.menuItemId).slice(4);
  const opts = {};
  if (info.mediaType === 'video' && info.srcUrl) opts.srcUrl = info.srcUrl;
  else if (info.linkUrl) opts.pageUrl = info.linkUrl;
  toast(tab.id, 'Sending to your TV…', true);
  try { const r = await cast(tab.id, device, opts); toast(tab.id, `Playing on ${r.device}`, true); }
  catch (e) { toast(tab.id, e.message, false); }
});

chrome.runtime.onInstalled.addListener(() => { menuSig = ''; refreshMenus([]); getState(); });
chrome.runtime.onStartup.addListener(() => getState());

// ------------------------------------------------------------------ messages from popup / page
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    switch (msg.type) {
      case 'state': return {state: await getState()};
      case 'start': return {state: await startServer()};
      case 'pageInfo': return await pageInfo(msg.tabId);
      case 'cast': {
        const tabId = msg.tabId ?? sender.tab.id;
        return await cast(tabId, msg.device, {video: msg.video || null, fromPopup: !!msg.fromPopup});
      }
      case 'openRemote': await openRemote(true, msg.hash || ''); return {};
      case 'control': return await api('/api/control', {device: msg.device, action: msg.action, value: msg.value});
      case 'open': {
        const tabs = await chrome.tabs.query({url: API + '/*'});
        if (tabs.length) { await chrome.tabs.update(tabs[0].id, {active: true}); await chrome.windows.update(tabs[0].windowId, {focused: true}); }
        else await chrome.tabs.create({url: API + '/'});
        return {ok: true};
      }
    }
    return {};
  })().then(r => reply(Object.assign({ok: true}, r)), e => reply({ok: false, error: e.message}));
  return true;   // reply asynchronously
});
