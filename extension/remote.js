// Shared TV remote, used by the popup and the pop-out remote window.
// mountRemote(element, {popout: bool, hideWhenIdle: bool, keys: bool, onState: fn})

const RM_API = 'http://localhost:8765';
const RM_ICON = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg><small>10</small>',
  fwd: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg><small>10</small>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>',
  minus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  vol: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  muted: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="m16 9 6 6M22 9l-6 6"/></svg>',
  popout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M19 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h4"/></svg>',
};

function mountRemote(root, opts = {}) {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const fmt = s => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`; };

  root.classList.add('rm');
  root.innerHTML = `
    <div class="rm-tabs" hidden></div>
    <div class="rm-idle" hidden></div>
    <div class="rm-card" hidden>
      <div class="rm-bg"></div>
      <div class="rm-top">
        <div class="rm-art"><img alt=""><span class="rm-live" hidden>LIVE</span><div class="rm-load"><div class="rm-ring"></div></div><div class="rm-nudge"></div></div>
        <div class="rm-meta"><div class="rm-on"><span class="rm-dev"></span><span class="rm-chip"></span></div>
          <div class="rm-title"></div><div class="rm-sub"></div></div>
        ${opts.popout ? `<button class="rm-pop" title="Open the remote in its own window">${RM_ICON.popout}</button>` : ''}
      </div>
      <div class="rm-track"><div class="rm-rail"></div><div class="rm-buf"></div><div class="rm-fill"></div><div class="rm-knob"></div><div class="rm-tip"></div></div>
      <div class="rm-times"><span class="rm-pos">0:00</span><span class="rm-dur"></span></div>
      <div class="rm-ctrls">
        <div class="rm-main">
          <button class="rm-cb rm-back" title="Back 10 seconds (←)">${RM_ICON.back}</button>
          <button class="rm-cb rm-big rm-pp" title="Play / pause (Space)">${RM_ICON.pause}</button>
          <button class="rm-cb rm-fwd" title="Forward 10 seconds (→)">${RM_ICON.fwd}</button>
          <button class="rm-cb rm-stop" title="Stop">${RM_ICON.stop}</button>
        </div>
        <div class="rm-vol">
          <button class="rm-cb rm-vdown" title="Volume down (↓)">${RM_ICON.minus}</button>
          <button class="rm-cb rm-mute" title="Mute (M)">${RM_ICON.vol}</button>
          <span class="rm-lvl"></span>
          <button class="rm-cb rm-vup" title="Volume up (↑)">${RM_ICON.plus}</button>
        </div>
      </div>
      <button class="rm-subbar" title="Subtitles: pick a track, add your own .srt, change the look">
        <span class="rm-ccbadge">CC</span><span class="rm-subtxt">Subtitles</span><span class="rm-chev">▾</span>
      </button>
      <div class="rm-msg" hidden></div>
      <div class="rm-ccp" hidden>
        <div class="rm-cch">Subtitles</div>
        <div class="rm-tracks"></div>
        <label class="rm-drop">
          <input type="file" accept=".srt,.vtt,.ass,.ssa,.sub,.txt" hidden>
          <span class="rm-drop-t">Drop an .srt file here or <u>choose one</u></span>
        </label>
        <div class="rm-prev"><div class="rm-prev-img"></div><span class="rm-prev-cap">This is how your subtitles will look</span></div>
        <div class="rm-sl"><span>Size</span><input type="range" data-k="size" min="12" max="40" step="1"><b data-v="size"></b></div>
        <div class="rm-sl"><span>Background</span><input type="range" data-k="bg" min="0" max="100" step="5"><b data-v="bg"></b></div>
        <div class="rm-sl"><span>Position</span><input type="range" data-k="pos" min="0" max="100" step="1"><b data-v="pos"></b></div>
        <div class="rm-sl"><span>Timing</span><input type="range" data-k="delay" min="-15" max="15" step="0.1"><b data-v="delay"></b></div>
        <div class="rm-cc-foot"><button class="rm-reset">Reset look</button><span class="rm-cc-hint">The TV updates when you let go (a quick reload).</span></div>
      </div>
    </div>`;
  const q = s => root.querySelector(s);

  let devices = [], sel = null, player = null, polledAt = 0, up = true;
  let pending = null, pendingTimer = null, rawBase = 0;
  let seekTarget = null, seekUntil = 0, stateHold = null, stateUntil = 0, dragging = false, msgTimer = null;

  async function api(path, body) {
    const r = await fetch(RM_API + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    return j;
  }
  function message(text) {
    const m = q('.rm-msg');
    m.textContent = text; m.hidden = !text;
    clearTimeout(msgTimer);
    if (text) msgTimer = setTimeout(() => { m.hidden = true; }, 6000);
  }

  // ---------------------------------------------------------------- polling
  async function poll() {
    let s;
    try { s = await api('/api/state' + (sel ? '?device=' + encodeURIComponent(sel) : '')); up = true; }
    catch (e) { up = false; devices = []; player = null; render(); opts.onState && opts.onState(null); return; }
    devices = s.devices;
    const playing = devices.filter(d => d.playing);
    const cur = devices.find(d => d.id === sel);
    if (!sel || !cur || (!cur.playing && playing.length && !(player && player.busy))) {
      const {lastDevice} = await chrome.storage.local.get('lastDevice');
      const next = (playing.find(d => d.id === lastDevice) || playing[0] || devices.find(d => d.id === lastDevice) || {}).id || null;
      if (next && next !== sel) { sel = next; return poll(); }
    }
    player = s.selected === sel ? s.player : null;
    polledAt = performance.now();
    render();
    opts.onState && opts.onState(s);
  }

  // ---------------------------------------------------------------- position & state (smoothed)
  function curPos() {
    if (!player) return 0;
    const now = performance.now();
    if (pending !== null) return pending;
    if (seekTarget !== null && now < seekUntil) {
      if (Math.abs((player.position || 0) - seekTarget) > 4 || player.state !== 'playing') return seekTarget;
      seekTarget = null;
    }
    let pos = player.position || 0;
    if (state() === 'playing') pos += (now - polledAt) / 1000;
    return player.duration ? Math.min(pos, player.duration) : pos;
  }
  function state() {
    if (!player) return 'stopped';
    if (stateHold && performance.now() < stateUntil) {
      if (player.state === stateHold) stateHold = null; else return stateHold;
    }
    return player.state;
  }

  // ---------------------------------------------------------------- render
  function render() {
    const dev = devices.find(d => d.id === sel);
    const active = player && (player.title || player.busy);
    const idle = q('.rm-idle'), card = q('.rm-card'), tabs = q('.rm-tabs');
    const playing = devices.filter(d => d.playing);
    tabs.hidden = playing.length < 2;
    if (!tabs.hidden) {
      const sig = playing.map(d => d.id + (d.id === sel)).join();
      if (tabs.dataset.sig !== sig) {
        tabs.dataset.sig = sig;
        tabs.innerHTML = playing.map(d => `<button class="rm-tab ${d.id === sel ? 'sel' : ''}" data-id="${esc(d.id)}">${esc(d.name)}</button>`).join('');
        tabs.querySelectorAll('.rm-tab').forEach(b => b.onclick = () => { sel = b.dataset.id; player = null; poll(); });
      }
    }
    if (opts.hideWhenIdle) root.hidden = !active;
    card.hidden = !active;
    idle.hidden = !!active || opts.hideWhenIdle;
    if (!active) {
      if (!up) {
        idle.innerHTML = 'Projecter isn\'t running.<br><button class="rm-start">Start Projecter</button>';
        idle.querySelector('.rm-start').onclick = async e => {
          e.target.textContent = 'Starting…'; e.target.disabled = true;
          try { await chrome.runtime.sendMessage({type: 'start'}); } catch (err) {}
          poll();
        };
      } else idle.textContent = 'Nothing is playing. Use “Project” on a video to send it to a TV.';
      return;
    }
    const st = state();
    root.classList.toggle('loading', !!player.busy || st === 'loading');
    if (player.title) {
      const img = q('.rm-art img');
      if (img.dataset.src !== (player.thumb || '')) {
        img.dataset.src = player.thumb || '';
        img.classList.toggle('none', !player.thumb);
        if (player.thumb) img.src = player.thumb; else img.removeAttribute('src');
        q('.rm-bg').style.backgroundImage = player.thumb ? `url("${player.thumb}")` : '';
      }
      q('.rm-title').textContent = player.title;
    }
    q('.rm-live').hidden = !player.live;
    q('.rm-dev').textContent = 'On ' + (dev ? dev.name : 'TV');
    q('.rm-chip').textContent = player.busy || ({playing: 'Playing', paused: 'Paused', loading: 'Loading…', stopped: 'Stopped', ended: 'Finished', unknown: 'Not responding'}[st] || st);
    q('.rm-sub').textContent = player.error || [player.site, player.quality, player.remote ? 'TV remote works too' : ''].filter(Boolean).join(' · ');
    q('.rm-pp').innerHTML = st === 'playing' || st === 'loading' ? RM_ICON.pause : RM_ICON.play;
    const canSeek = !!player.can_seek && !player.busy;
    q('.rm-back').disabled = q('.rm-fwd').disabled = !canSeek;
    q('.rm-track').classList.toggle('locked', !canSeek);
    q('.rm-mute').innerHTML = player.muted ? RM_ICON.muted : RM_ICON.vol;
    q('.rm-lvl').textContent = player.volume ?? '';
    q('.rm-lvl').hidden = player.volume == null;
    if (player.notice && player.notice !== lastNotice) { lastNotice = player.notice; message(player.notice); }
    renderSubs();
  }

  // ---------------------------------------------------------------- subtitles panel
  const SUB_DEFAULT = {size: 20, bg: 0, pos: 6, delay: 0};
  let subStyle = Object.assign({}, SUB_DEFAULT), lastNotice = '', styleTimer = null, styleTouched = 0, liveTimer = null, liveSent = 0;
  chrome.storage.sync.get('subStyle').then(s => { subStyle = Object.assign({}, SUB_DEFAULT, s.subStyle || {}); syncSliders(); });

  function renderSubs() {
    const ok = player && player.subs_ok;
    const bar = q('.rm-subbar'), list = (player && player.subtitles) || [];
    bar.disabled = !ok;
    bar.classList.toggle('on', !!ok && player.sub >= 0);
    q('.rm-subtxt').textContent = !ok ? 'Subtitles aren’t available for this video'
      : player.sub >= 0 ? `Subtitles: ${list[player.sub]}`
      : list.length ? `Subtitles: Off · ${list.length} found on the page` : 'Subtitles: Off · add your own .srt';
    q('.rm-chev').textContent = q('.rm-ccp').hidden ? '▾' : '▴';
    if (!ok) { q('.rm-ccp').hidden = true; return; }
    // follow the style the TV is actually using (it may have been changed on the web page)
    if (player.sub_style && Date.now() - styleTouched > 15000 && JSON.stringify(player.sub_style) !== JSON.stringify(subStyle)) {
      subStyle = Object.assign({}, SUB_DEFAULT, player.sub_style); syncSliders();
    }
    const tr = q('.rm-tracks');
    const sig = JSON.stringify([player.subtitles, player.sub]);
    if (tr.dataset.sig === sig) return;
    tr.dataset.sig = sig;
    const items = [['Off', -1]].concat((player.subtitles || []).map((l, i) => [l, i]));
    tr.innerHTML = items.map(([l, i]) => `<button class="rm-trk ${player.sub === i ? 'sel' : ''}" data-i="${i}">${esc(l)}</button>`).join('');
    tr.querySelectorAll('.rm-trk').forEach(b => b.onclick = () => {
      if (+b.dataset.i === player.sub) return;
      control('subs', +b.dataset.i);
    });
  }

  function syncSliders() {
    root.querySelectorAll('.rm-sl input').forEach(i => { i.value = subStyle[i.dataset.k]; });
    preview();
  }
  function preview() {
    const v = k => root.querySelector(`[data-v="${k}"]`);
    v('size').textContent = subStyle.size;
    v('bg').textContent = subStyle.bg + '%';
    v('pos').textContent = subStyle.pos <= 2 ? 'Bottom' : subStyle.pos >= 95 ? 'Top' : subStyle.pos + '%';
    v('delay').textContent = (subStyle.delay > 0 ? '+' : '') + Number(subStyle.delay).toFixed(1) + 's';
    // same maths as the server: libass draws on a 288-line canvas
    const box = q('.rm-prev'), H = box.clientHeight || 120, cap = q('.rm-prev-cap');
    cap.style.fontSize = (subStyle.size / 288 * H) + 'px';
    cap.style.bottom = ((8 + subStyle.pos / 100 * 230) / 288 * H) + 'px';
    cap.style.background = subStyle.bg > 0 ? `rgba(0,0,0,${subStyle.bg / 100})` : 'transparent';
    cap.style.textShadow = subStyle.bg > 0 ? 'none' : '0 0 2px #000,0 0 2px #000,0 1px 3px #000';
    if (player && player.thumb) q('.rm-prev-img').style.backgroundImage = `url("${player.thumb}")`;
  }
  root.querySelectorAll('.rm-sl input').forEach(i => {
    i.addEventListener('input', () => {
      subStyle[i.dataset.k] = parseFloat(i.value); styleTouched = Date.now(); preview();
      // Projecter's Roku channel draws subtitles itself: send while dragging (~8 updates a second)
      if (player && player.live_subs) {
        clearTimeout(liveTimer);
        const wait = Math.max(0, 120 - (Date.now() - liveSent));
        liveTimer = setTimeout(() => { liveSent = Date.now(); control('subs_style', Object.assign({}, subStyle)); }, wait);
      }
    });
    i.addEventListener('change', () => {
      chrome.storage.sync.set({subStyle});
      clearTimeout(styleTimer);
      if (player && player.live_subs) return;   // already sent while dragging
      // burned-in subtitles: several quick adjustments = one reload on the TV
      styleTimer = setTimeout(() => control('subs_style', subStyle), 700);
    });
  });
  q('.rm-reset').onclick = () => {
    subStyle = Object.assign({}, SUB_DEFAULT); styleTouched = Date.now(); syncSliders();
    chrome.storage.sync.set({subStyle}); control('subs_style', subStyle);
  };
  q('.rm-subbar').onclick = () => {
    const p = q('.rm-ccp'); p.hidden = !p.hidden;
    q('.rm-chev').textContent = p.hidden ? '▾' : '▴';
    if (!p.hidden) syncSliders();
  };

  async function addFile(file) {
    if (!file) return;
    if (file.size > 3e6) return message('That file is too big to be subtitles.');
    const text = await file.text();
    if (!/-->/.test(text) && !/\[Script Info\]/.test(text)) return message("That doesn't look like a subtitle file (.srt or .vtt).");
    q('.rm-drop-t').textContent = `Adding ${file.name}…`;
    await control('subs_add', {label: file.name.replace(/\.[^.]+$/, ''), text});
    q('.rm-drop-t').innerHTML = 'Drop an .srt file here or <u>choose one</u>';
  }
  const drop = q('.rm-drop'), input = drop.querySelector('input');
  if (opts.inPopup) {
    // a file picker closes the popup, so files are added in the remote window instead
    drop.querySelector('.rm-drop-t').innerHTML = 'Add your own .srt file… <u>open the remote</u>';
    drop.onclick = e => { e.preventDefault(); chrome.runtime.sendMessage({type: 'openRemote', hash: '#cc'}).then(() => window.close()); };
  } else {
    input.onchange = () => addFile(input.files[0]);
    const target = opts.dropTarget || drop;
    target.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
    target.addEventListener('dragleave', e => { if (e.target === target || e.target === drop) drop.classList.remove('over'); });
    target.addEventListener('drop', e => {
      e.preventDefault(); drop.classList.remove('over');
      if (q('.rm-ccp').hidden) q('.rm-subbar').click();
      addFile(e.dataTransfer.files[0]);
    });
  }
  if (opts.openSubs) setTimeout(function openWhenReady() {
    if (player && player.subs_ok) { q('.rm-ccp').hidden = false; syncSliders(); } else setTimeout(openWhenReady, 400);
  }, 300);

  function frame() {
    if (player && player.title && !q('.rm-card').hidden) {
      const dur = player.duration || 0, pos = curPos();
      const pct = dur ? Math.min(100, pos / dur * 100) : 0;
      if (!dragging) {
        q('.rm-fill').style.width = pct + '%'; q('.rm-knob').style.left = pct + '%';
        q('.rm-pos').textContent = player.live ? 'LIVE' : fmt(pos);
      }
      q('.rm-buf').style.width = Math.min(100, (player.cached || 0) * 100) + '%';
      q('.rm-dur').textContent = player.live ? '' : fmt(dur);
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // ---------------------------------------------------------------- actions
  async function control(action, value) {
    if (!sel) return;
    try { await api('/api/control', {device: sel, action, value}); setTimeout(poll, 250); }
    catch (e) { message(e.message); poll(); }
  }
  function togglePlay() {
    if (!player || !player.title) return;
    const next = state() === 'playing' ? 'paused' : 'playing';
    stateHold = next; stateUntil = performance.now() + 4000; render();
    control(next === 'paused' ? 'pause' : 'play');
  }
  function nudge(delta) {
    if (!player || !player.can_seek || player.busy) return;
    if (pending === null) rawBase = curPos();
    pending = Math.max(0, Math.min((player.duration || 0) - 3, (pending ?? rawBase) + delta));
    const diff = Math.round(pending - rawBase);
    const n = q('.rm-nudge'); n.textContent = (diff >= 0 ? '+' : '−') + Math.abs(diff) + 's';
    n.classList.remove('pop'); void n.offsetWidth; n.classList.add('pop');
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => {
      seekTarget = pending; seekUntil = performance.now() + 15000; pending = null;
      control('seek', seekTarget);
    }, 550);
  }
  function seekTo(t) { pending = null; seekTarget = t; seekUntil = performance.now() + 15000; control('seek', t); }

  q('.rm-pp').onclick = togglePlay;
  q('.rm-back').onclick = () => nudge(-10);
  q('.rm-fwd').onclick = () => nudge(10);
  q('.rm-stop').onclick = () => { control('stop'); player = null; render(); };
  q('.rm-vup').onclick = () => control('vol_up');
  q('.rm-vdown').onclick = () => control('vol_down');
  q('.rm-mute').onclick = () => control('mute');
  if (opts.popout) q('.rm-pop').onclick = () => chrome.runtime.sendMessage({type: 'openRemote'}).then(() => window.close());

  const track = q('.rm-track');
  const tAt = e => { const r = track.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * ((player && player.duration) || 0); };
  track.addEventListener('pointermove', e => {
    const t = tAt(e), r = track.getBoundingClientRect();
    q('.rm-tip').style.left = Math.min(r.width, Math.max(0, e.clientX - r.left)) + 'px';
    q('.rm-tip').textContent = fmt(t);
    if (dragging) { const pct = t / (player.duration || 1) * 100; q('.rm-fill').style.width = pct + '%'; q('.rm-knob').style.left = pct + '%'; q('.rm-pos').textContent = fmt(t); }
  });
  track.addEventListener('pointerdown', e => {
    if (!player || !player.can_seek || player.busy) return;
    dragging = true; track.classList.add('drag'); track.setPointerCapture(e.pointerId);
  });
  track.addEventListener('pointerup', e => { if (!dragging) return; dragging = false; track.classList.remove('drag'); seekTo(tAt(e)); });

  if (opts.keys) {
    document.addEventListener('keydown', e => {
      if (e.target.matches('input,textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key;
      if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
      else if (k === 'ArrowLeft' || k === 'j') { e.preventDefault(); nudge(-10); }
      else if (k === 'ArrowRight' || k === 'l') { e.preventDefault(); nudge(10); }
      else if (k === 'ArrowUp') { e.preventDefault(); control('vol_up'); }
      else if (k === 'ArrowDown') { e.preventDefault(); control('vol_down'); }
      else if (k === 'm') control('mute');
    });
  }

  poll();
  const timer = setInterval(poll, 1000);
  return {poll, select(id) { sel = id; player = null; poll(); }, stop() { clearInterval(timer); }};
}
