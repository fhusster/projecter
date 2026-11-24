const $ = id => document.getElementById(id);
const send = msg => chrome.runtime.sendMessage(msg).then(r => { if (!r || !r.ok) throw new Error(r ? r.error : 'No reply'); return r; });
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const fmt = s => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`; };
const ICON = {
  tv: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="13" rx="2.5"/><path d="M8 21h8"/></svg>',
  roku: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="13" rx="2.5"/><path d="M8 21h8"/><path d="m10 9 4 2.5-4 2.5z" fill="currentColor"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
};

let tab = null, state = null, casting = null;

function showError(msg, ok) {
  const e = $('err');
  e.className = ok ? 'ok' : 'err';
  e.textContent = msg || '';
  e.hidden = !msg;
}

// ---------------------------------------------------------------- page
async function loadPage() {
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  $('ptitle').textContent = tab.title || tab.url;
  let info = null;
  try { info = await send({type: 'pageInfo', tabId: tab.id}); } catch (e) {}
  const d = $('pdetail');
  if (!info || !/^https?:/.test(tab.url)) { d.innerHTML = '<span class="tag warn">Nothing to project here</span>'; return; }
  const img = (info.video && info.video.poster) || info.image || (tab.favIconUrl && !info.video ? '' : '');
  if (img) { $('thumb').style.backgroundImage = `url("${img}")`; $('thumb').classList.add('img'); }
  const bits = [];
  if (info.video) {
    bits.push('<span class="tag">Video found</span>');
    if (info.video.duration) bits.push(`${fmt(info.video.time)} / ${fmt(info.video.duration)}`);
  } else if (info.streams.length) {
    bits.push('<span class="tag">Stream found</span>');
  } else {
    bits.push('<span class="tag warn">No video playing</span>', 'will try the page link');
  }
  if (info.streams.length) {
    const kinds = [...new Set(info.streams.map(s => ({hls: 'HLS', dash: 'DASH', file: 'MP4'}[s.kind])))];
    bits.push(kinds.join(' · '));
  }
  if (info.subs) bits.push('<span class="tag">CC</span>');
  d.innerHTML = bits.join(' ');
}

// ---------------------------------------------------------------- TVs
function renderTVs() {
  const box = $('tvs');
  if (!state) { box.innerHTML = '<div class="empty">Start Projecter to see your TVs.</div>'; return; }
  if (!state.devices.length) { box.innerHTML = '<div class="empty">No TVs found. Make sure they\'re on.</div>'; return; }
  box.innerHTML = '';
  for (const d of state.devices) {
    const el = document.createElement('div');
    el.className = `tv ${d.kind} ${d.status === 'offline' ? 'off' : ''}`;
    const st = d.playing ? '<span class="eq"><i></i><i></i><i></i></span>Playing'
      : `<span class="dot ${d.status}"></span>${{online: 'Ready', standby: 'Standby · will turn on', offline: 'Off'}[d.status]}`;
    const busy = casting === d.id;
    el.innerHTML = `<div class="row"><span class="ic">${d.kind === 'roku' ? ICON.roku : ICON.tv}</span>
        <span style="min-width:0"><div class="nm">${esc(d.name)}</div><div class="st">${st}</div></span>
        <button class="cast ${busy ? 'busy' : ''}" ${d.status === 'offline' || casting ? 'disabled' : ''}>${busy ? '<span class="spin"></span>Sending' : ICON.play + 'Project'}</button></div>`;
    el.querySelector('.cast').onclick = () => castTo(d);
    box.appendChild(el);
  }
}

async function castTo(d) {
  casting = d.id; showError(''); renderTVs();
  try {
    const r = await send({type: 'cast', tabId: tab.id, device: d.id, fromPopup: true});
    showError(`Playing on ${r.device}`, true);
    remote.select(d.id);   // the remote at the top now controls this TV
  } catch (e) { showError(e.message); }
  finally { casting = null; refresh(); }
}

// ---------------------------------------------------------------- connection
async function refresh() {
  let r = null;
  try { r = await send({type: 'state'}); } catch (e) {}
  state = r && r.state;
  const c = $('conn');
  c.className = 'conn ' + (state ? 'on' : 'off');
  c.querySelector('span').textContent = state ? 'Connected' : 'Not running';
  $('down').hidden = !!state;
  renderTVs();
}

$('start').onclick = async () => {
  const b = $('start'); b.disabled = true; b.textContent = 'Starting…';
  try { await send({type: 'start'}); showError(''); }
  catch (e) { showError(e.message); }
  finally { b.disabled = false; b.textContent = 'Start Projecter'; refresh(); }
};
$('open').onclick = () => send({type: 'open'}).then(() => window.close());

// settings
const DEFAULTS = {resume: true, pauseBrowser: true, overlay: true, autoRemote: true, autoSubs: true};
chrome.storage.sync.get(DEFAULTS).then(s => {
  for (const k of Object.keys(DEFAULTS)) {
    $(k).checked = s[k];
    $(k).onchange = () => chrome.storage.sync.set({[k]: $(k).checked});
  }
});

const remote = mountRemote($('remote'), {popout: true, hideWhenIdle: true, keys: true, inPopup: true});
loadPage();
refresh();
setInterval(refresh, 2000);
