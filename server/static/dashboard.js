'use strict';

const $ = (s, r = document) => r.querySelector(s);
const STALE_SEC = 15;

const state = {
  devices: new Map(),   // id -> device info
  frames: new Map(),    // id -> {url, prev}
  tiles: new Map(),     // id -> tile element
  pending: [],
  cols: store('cols') === null ? 0 : Number(store('cols')),  // 0 = 자동
  fit: store('fit') || 'cover',
  filter: '',
  focused: null,        // 확대 보기 중인 PC
  sentFocus: new Set(), // 서버에 고화질(focus)을 요청해 둔 PC
  loaded: false,
  ws: null,
  serverOffset: 0,      // 서버 시각 - 브라우저 시각
};

function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem('sm.' + k);
    localStorage.setItem('sm.' + k, v);
  } catch { return null; }
}

// 화면 전송 방식 (0 = 실시간 스트리밍, N = N초마다 한 장 캡처)
const INTERVALS = [
  [0, '실시간 스트리밍 (1fps)'], [5, '5초마다 캡처'], [10, '10초마다 캡처'], [30, '30초마다 캡처'],
  [60, '1분마다 캡처'], [300, '5분마다 캡처'], [600, '10분마다 캡처'],
];
function fmtIv(s) {
  if (s % 60 === 0 && s >= 60) return `${s / 60}분`;
  return `${s}초`;
}
function modeText(d) {
  const iv = d.eff_interval || 0;
  if (!iv) return d.focused ? '실시간 (확대 보기)' : '실시간';
  return `${fmtIv(iv)}마다 캡처` + (d.client_interval && d.client_interval >= (d.interval || 0) ? ' (PC 저부하 모드)' : '');
}
function fillIntervalSelect(sel, value) {
  const opts = INTERVALS.some(([v]) => v === value) ? INTERVALS : [...INTERVALS, [value, `${fmtIv(value)}마다 캡처`]];
  sel.replaceChildren(...opts.map(([v, t]) => new Option(t, v)));
  sel.value = String(value);
}
const ICON_CLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
const ICON_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4M2 2l20 20"/></svg>';
const ICON_WAIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>';

/* ------------------------------------------------------------------ */
/* 유틸                                                                */
/* ------------------------------------------------------------------ */
function ipKey(ip) {
  const p = ip.split('.');
  return p.length === 4 && p.every(x => /^\d+$/.test(x)) ? p.map(x => x.padStart(3, '0')).join('.') : ip;
}
function label(d) { return d.name || d.hostname || d.ip; }
function now() { return Date.now() / 1000 + state.serverOffset; }
function ago(t) {
  if (!t) return '-';
  const s = Math.max(0, Math.round(now() - t));
  if (s < 60) return `${s}초 전`;
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}
function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), 4000);
}
async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('로그인이 필요합니다'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : '요청에 실패했습니다');
  return j;
}
function isStale(d) {
  // 주기 캡처 PC 는 주기 + 여유 시간 동안 새 화면이 없어야 '응답 없음'
  const limit = Math.max(d.interval || 0, d.client_interval || 0) + STALE_SEC;
  return d.online && d.last_frame_at && now() - d.last_frame_at > limit;
}

/* ------------------------------------------------------------------ */
/* CCTV 월 렌더링                                                       */
/* ------------------------------------------------------------------ */
function sortedDevices() {
  const f = state.filter.trim().toLowerCase();
  return [...state.devices.values()]
    .filter(d => !f || [d.name, d.ip, d.hostname, d.user].some(v => (v || '').toLowerCase().includes(f)))
    .sort((a, b) => ipKey(a.ip).localeCompare(ipKey(b.ip)));
}

function makeTile(d) {
  const el = document.createElement('div');
  el.className = 'tile';
  el.dataset.id = d.id;
  el.innerHTML = `
    <img alt="" hidden>
    <div class="placeholder"></div>
    <div class="label"><span class="dot"></span><span class="nm"></span><span class="ip"></span><span class="mode" hidden></span></div>
    <div class="corner"></div>`;
  el.addEventListener('click', () => openViewer(d.id));
  return el;
}

function updateTile(id) {
  const d = state.devices.get(id);
  const el = state.tiles.get(id);
  if (!d || !el) return;
  const fr = state.frames.get(id);
  const img = el.querySelector('img');
  el.classList.toggle('offline', !d.online);
  el.classList.toggle('stale', isStale(d));
  el.querySelector('.nm').textContent = label(d);
  el.querySelector('.ip').textContent = d.name || d.hostname ? d.ip : '';
  el.title = [label(d), d.ip, d.hostname, d.user && `사용자: ${d.user}`].filter(Boolean).join(' · ');
  img.hidden = !fr;

  const ph = el.querySelector('.placeholder');
  let phHtml = '';
  if (!d.online) {
    phHtml = fr ? `<span class="msg">오프라인 · ${ago(d.last_seen)}</span>` : `${ICON_OFF}<span>오프라인</span>`;
  } else if (!fr) {
    phHtml = `${ICON_WAIT}<span>화면 수신 대기 중</span>`;
  }
  if (ph.dataset.html !== phHtml) { ph.innerHTML = phHtml; ph.dataset.html = phHtml; }

  const iv = d.eff_interval || 0;
  const mode = el.querySelector('.mode');
  const modeHtml = iv ? `${ICON_CLOCK}${fmtIv(iv)}` : '';
  if (mode.dataset.html !== modeHtml) { mode.innerHTML = modeHtml; mode.dataset.html = modeHtml; }
  mode.hidden = !iv;
  mode.title = iv ? modeText(d) : '';

  el.querySelector('.corner').textContent = !d.online
    ? `마지막 접속 ${ago(d.last_seen)}`
    : iv ? `${fmtIv(iv)} 주기 · 마지막 캡처 ${ago(d.last_frame_at)}`
         : `${d.fps ?? 0} fps · ${ago(d.last_frame_at)}`;
}

// 자동 모드: 16:9 타일이 화면을 최대한 크게 채우는 열 수 계산 (스크롤 없이 한 화면에)
function layoutWall(n) {
  const wall = $('#wall');
  const wrap = $('.wall-wrap');
  if (state.cols > 0 || n === 0) {
    wall.style.setProperty('--cols', state.cols || 4);
    wall.style.gridTemplateColumns = '';
    wall.style.width = '';
    return;
  }
  const W = wrap.clientWidth, H = wrap.clientHeight, gap = 2;
  let best = {w: 0, c: 1};
  for (let c = 1; c <= n; c++) {
    const rows = Math.ceil(n / c);
    const w = Math.min((W - gap * (c - 1)) / c, ((H - gap * (rows - 1)) / rows) * 16 / 9);
    if (w > best.w) best = {w, c};
  }
  const tw = Math.floor(best.w);
  wall.style.gridTemplateColumns = `repeat(${best.c}, ${tw}px)`;
  wall.style.width = `${tw * best.c + gap * (best.c - 1)}px`;
}

function renderWall() {
  const wall = $('#wall');
  wall.classList.toggle('fit', state.fit === 'contain');
  const list = sortedDevices();
  layoutWall(list.length);
  const want = new Set(list.map(d => d.id));
  for (const [id, el] of state.tiles) {
    if (!want.has(id)) { el.remove(); state.tiles.delete(id); }
  }
  list.forEach((d, i) => {
    let el = state.tiles.get(d.id);
    if (!el) {
      el = makeTile(d);
      state.tiles.set(d.id, el);
      const fr = state.frames.get(d.id);
      if (fr) el.querySelector('img').src = fr.url;
    }
    if (wall.children[i] !== el) wall.insertBefore(el, wall.children[i] || null);
    updateTile(d.id);
  });
  $('#empty').hidden = state.devices.size > 0;
  updateStats();
}

function updateStats() {
  const all = [...state.devices.values()];
  const on = all.filter(d => d.online).length;
  $('#stOnline').textContent = on;
  $('#stOffline').textContent = all.length - on;
  $('#stTotal').textContent = all.length;
  document.title = `(${on}/${all.length}) Screen Monitor`;
}

function setFrame(id, blob) {
  const url = URL.createObjectURL(blob);
  const prev = state.frames.get(id);
  // 이전-이전 URL만 해제 (아직 로딩 중인 이미지가 깨지지 않도록)
  if (prev && prev.prev) URL.revokeObjectURL(prev.prev);
  state.frames.set(id, {url, prev: prev ? prev.url : null});
  const el = state.tiles.get(id);
  if (el) {
    const img = el.querySelector('img');
    img.src = url;
    if (img.hidden) updateTile(id);
  }
  if (state.focused === id) {
    $('#vImg').src = url;
    $('#vImg').hidden = false;
    $('#vPh').hidden = true;
  }
  if (slide.on) {
    const th = thumbs.get(id);
    if (th) { const ti = th.querySelector('img'); ti.src = url; ti.hidden = false; }
    if (slide.id === id) {
      const si = $('#sImg');
      si.src = url;
      if (si.hidden) renderSlide(false);
    }
  }
}

// 확대 보기 / 슬라이드 쇼가 보고 있는 PC만 서버에 고화질(focus)로 요청한다
function syncFocus() {
  const want = new Set();
  if (state.focused) want.add(state.focused);
  if (slide.on && slide.hq && slide.id) want.add(slide.id);
  for (const id of state.sentFocus) if (!want.has(id)) send({type: 'unfocus', id});
  for (const id of want) if (!state.sentFocus.has(id)) send({type: 'focus', id});
  state.sentFocus = want;
}

/* ------------------------------------------------------------------ */
/* WebSocket                                                          */
/* ------------------------------------------------------------------ */
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/view`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  const dec = new TextDecoder();

  ws.onopen = () => {
    setConn(true);
    state.sentFocus = new Set();  // 새 연결에는 focus 상태가 없으므로 다시 보낸다
    syncFocus();
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== 'string') {
      const u8 = new Uint8Array(e.data);
      const n = u8[0];
      const id = dec.decode(u8.subarray(1, 1 + n));
      if (state.devices.has(id)) setFrame(id, new Blob([u8.subarray(1 + n)], {type: 'image/jpeg'}));
      return;
    }
    handle(JSON.parse(e.data));
  };
  ws.onclose = async (e) => {
    setConn(false);
    if (e.code === 4001) { location.href = '/login'; return; }
    try {
      const r = await fetch('/api/me');
      if (r.status === 401) { location.href = '/login'; return; }
    } catch { /* 서버 다운 - 재시도 */ }
    setTimeout(connect, 2000);
  };
}
function send(obj) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
}
function setConn(ok) {
  $('#conn').classList.toggle('ok', ok);
  $('#conn .txt').textContent = ok ? '실시간' : '재연결 중';
}

function handle(m) {
  if (m.now) state.serverOffset = m.now - Date.now() / 1000;
  switch (m.type) {
    case 'snapshot':
      state.loaded = true;
      state.devices = new Map(m.devices.map(d => [d.id, d]));
      for (const id of [...state.frames.keys()]) if (!state.devices.has(id)) state.frames.delete(id);
      setPending(m.pending || []);
      renderWall();
      renderManage();
      break;
    case 'device': {
      const d = m.device;
      const old = state.devices.get(d.id);
      if (!d.registered) { state.devices.delete(d.id); renderWall(); renderManage(); break; }
      state.devices.set(d.id, {...old, ...d});
      if (m.event === 'online') toast(`${label(d)} (${d.ip}) 접속`, 'ok');
      if (m.event === 'offline') toast(`${label(d)} (${d.ip}) 연결 끊김`, 'bad');
      if (!old || label(old) !== label(d)) renderWall(); else { updateTile(d.id); updateStats(); }
      renderManage();
      if (state.focused === d.id) updateViewerMeta();
      break;
    }
    case 'remove':
      state.devices.delete(m.id);
      state.frames.delete(m.id);
      if (state.focused === m.id) closeViewer();
      renderWall();
      renderManage();
      break;
    case 'stats':
      for (const [id, s] of Object.entries(m.stats)) {
        const d = state.devices.get(id);
        if (d) Object.assign(d, s);
      }
      for (const id of state.tiles.keys()) updateTile(id);
      if (state.focused) updateViewerMeta();
      break;
    case 'pending':
      setPending(m.pending);
      if (m.new) toast(`등록되지 않은 PC가 접속을 시도했습니다: ${m.new}`, 'warn');
      break;
  }
  if (m.type !== 'pending') slideSync();
}

/* ------------------------------------------------------------------ */
/* 확대 보기                                                            */
/* ------------------------------------------------------------------ */
function openViewer(id) {
  state.focused = id;
  syncFocus();
  const fr = state.frames.get(id);
  $('#vImg').hidden = !fr;
  if (fr) $('#vImg').src = fr.url;
  $('#vPh').hidden = !!fr;
  updateViewerMeta();
  $('#viewer').classList.add('open');
}
function closeViewer() {
  state.focused = null;
  syncFocus();
  $('#viewer').classList.remove('open');
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}
function stepViewer(dir) {
  const list = sortedDevices();
  if (!list.length || !state.focused) return;
  const i = list.findIndex(d => d.id === state.focused);
  openViewer(list[(i + dir + list.length) % list.length].id);
}
function updateViewerMeta() {
  const d = state.devices.get(state.focused);
  if (!d) return;
  $('#vTitle').textContent = label(d);
  const parts = [d.ip];
  if (d.hostname) parts.push(d.hostname);
  if (d.user) parts.push(`사용자 ${d.user}`);
  if (d.screen) parts.push(`${d.screen[0]}×${d.screen[1]}`);
  if (!d.online) parts.push(`오프라인 (${ago(d.last_seen)})`);
  else if (d.eff_interval) parts.push(`${modeText(d)} · 마지막 캡처 ${ago(d.last_frame_at)}`);
  else parts.push(`${d.fps ?? 0} fps · ${d.kbps ?? 0} kbps`);
  $('#vMeta').textContent = parts.join(' · ');
  if (!state.frames.get(d.id)) $('#vPh').textContent = d.online ? '화면 수신 대기 중…' : '오프라인';
}

$('#vClose').onclick = closeViewer;
$('#vPrev').onclick = (e) => { e.stopPropagation(); stepViewer(-1); };
$('#vNext').onclick = (e) => { e.stopPropagation(); stepViewer(1); };
$('#vCapture').onclick = () => {
  const d = state.devices.get(state.focused);
  if (!d || !d.online) return toast('오프라인 PC는 캡처할 수 없습니다', 'warn');
  send({type: 'capture', id: d.id});
  toast(`${label(d)} 화면 캡처를 요청했습니다`);
};
$('#vSnap').onclick = () => {
  if (!state.focused || !state.frames.get(state.focused)) return toast('저장할 화면이 없습니다', 'warn');
  const a = document.createElement('a');
  a.href = `/api/devices/${encodeURIComponent(state.focused)}/snapshot`;
  a.click();
};
$('#vFull').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else $('#viewer').requestFullscreen().catch(() => {});
};
document.addEventListener('keydown', (e) => {
  const open = document.querySelectorAll('.overlay.open');
  if (open.length) {
    if (e.key === 'Escape') open.forEach(o => o.classList.remove('open'));
    return;
  }
  const t = e.target instanceof Element ? e.target : document.body;
  if (t.matches('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (state.focused) {
    if (e.key === 'Escape' && !document.fullscreenElement) closeViewer();
    if (e.key === 'ArrowLeft') stepViewer(-1);
    if (e.key === 'ArrowRight') stepViewer(1);
    if (k === 'f') $('#vFull').click();
    return;
  }
  if (k === 'g') return setView('grid');
  if (k === 's') return setView('slide');
  if (!slide.on) return;
  if (e.key === 'ArrowLeft') slideGo(-1);
  else if (e.key === 'ArrowRight') slideGo(1);
  else if (e.key === ' ' && !t.closest('button')) { e.preventDefault(); setPlaying(!slide.playing); }
  else if (e.key === 'Enter' && !t.closest('button')) $('#sOpen').click();
  else if (k === 'f') $('#sFull').click();
});

/* ------------------------------------------------------------------ */
/* 슬라이드 쇼: PC 화면을 한 대씩 크게, 일정 시간마다 자동으로 넘긴다       */
/* ------------------------------------------------------------------ */
const DWELLS = [[5, '5초'], [10, '10초'], [15, '15초'], [30, '30초'], [60, '1분'], [120, '2분'], [300, '5분']];
const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
const slide = {
  on: false,
  id: null,
  playing: store('slidePlay') !== '0',
  dwell: DWELLS.some(([v]) => v === Number(store('slideDwell'))) ? Number(store('slideDwell')) : 10,
  skipOff: store('slideSkipOff') !== '0',
  hq: store('slideHq') !== '0',
  timer: 0, startedAt: 0, remain: 0,
};
const thumbs = new Map();  // id -> 하단 썸네일 버튼

function slideOk(d) { return !!d && (!slide.skipOff || d.online); }
function slideCandidates() { return sortedDevices().filter(slideOk); }
// 현재 PC 기준 dir 방향으로 다음에 보여줄 PC (현재 PC가 사라졌어도 정렬 순서 기준으로 이어서)
function slidePick(dir) {
  const full = sortedDevices();
  const n = full.length;
  if (!n) return null;
  let i = full.findIndex(d => d.id === slide.id);
  if (i < 0) i = dir > 0 ? -1 : 0;
  for (let k = 1; k <= n; k++) {
    const d = full[(((i + dir * k) % n) + n) % n];
    if (slideOk(d)) return d.id;
  }
  return null;
}

function setView(v) {
  const on = v === 'slide';
  if (on === slide.on && $('#slides').hidden === !on) return;
  slide.on = on;
  store('view', v);
  document.body.classList.toggle('mode-slide', on);
  $('.wall-wrap').hidden = on;
  $('#slides').hidden = !on;
  syncSeg();
  if (on) {
    if (!slideOk(state.devices.get(slide.id))) slide.id = slidePick(1);
    showSlide(slide.id);
  } else {
    stopTimer();
    if (document.fullscreenElement === $('#slides')) document.exitFullscreen().catch(() => {});
    syncFocus();
    renderWall();
  }
}

function showSlide(id) {
  const changed = id !== slide.id;
  slide.id = id;
  syncFocus();
  renderSlide(changed);
  renderStrip();
  const cur = thumbs.get(id);
  if (cur) cur.scrollIntoView({block: 'nearest', inline: 'center'});
  restartTimer();
}
function slideGo(dir) { showSlide(slidePick(dir)); }

// 장치 목록/상태가 바뀔 때 호출: 사라졌거나 오프라인이 된 PC는 바로 다음으로 넘긴다
function slideSync() {
  if (!slide.on) return;
  if (!slideOk(state.devices.get(slide.id))) {
    const next = slidePick(1);
    if (next !== slide.id) return showSlide(next);
  }
  renderSlide(false);
  renderStrip();
}

function renderSlide(fade) {
  const d = state.devices.get(slide.id);
  const stage = $('#sStage'), img = $('#sImg'), ph = $('#sPh');
  const fr = d && state.frames.get(d.id);
  img.hidden = !fr;
  if (fr && img.src !== fr.url) img.src = fr.url;
  if (fade && fr) { img.classList.remove('fade'); void img.offsetWidth; img.classList.add('fade'); }
  stage.classList.toggle('none', !d);
  stage.classList.toggle('offline', !!d && !d.online);
  stage.classList.toggle('stale', !!d && isStale(d));

  let phHtml = '';
  if (!d) {
    phHtml = !state.loaded ? `${ICON_WAIT}<span>연결 중…</span>`
      : state.devices.size === 0 ? `${ICON_WAIT}<span>모니터링할 PC가 없습니다. PC 관리에서 IP를 등록하세요.</span>`
      : sortedDevices().length === 0 ? `${ICON_WAIT}<span>검색 조건에 맞는 PC가 없습니다.</span>`
      : `${ICON_OFF}<span>지금 온라인인 PC가 없습니다.<br>PC가 접속하면 자동으로 표시합니다.</span>`;
  } else if (!d.online) {
    phHtml = fr ? `<span class="msg">오프라인 · ${ago(d.last_seen)}</span>` : `${ICON_OFF}<span>오프라인</span>`;
  } else if (!fr) {
    phHtml = `${ICON_WAIT}<span>화면 수신 대기 중…</span>`;
  }
  if (ph.dataset.html !== phHtml) { ph.innerHTML = phHtml; ph.dataset.html = phHtml; }

  const list = slideCandidates();
  const idx = d ? list.findIndex(x => x.id === d.id) : -1;
  $('#sCount').textContent = `${idx < 0 ? '-' : idx + 1} / ${list.length}` + (slide.playing ? '' : ' · 일시정지');
  if (!d) { $('#sMeta').textContent = ''; return; }

  $('#sName').textContent = label(d);
  $('#sIp').textContent = d.name || d.hostname ? d.ip : '';
  const iv = d.eff_interval || 0;
  const mode = $('#sMode');
  const modeHtml = iv ? `${ICON_CLOCK}${fmtIv(iv)}` : '';
  if (mode.dataset.html !== modeHtml) { mode.innerHTML = modeHtml; mode.dataset.html = modeHtml; }
  mode.hidden = !iv;
  mode.title = iv ? modeText(d) : '';

  const parts = [];
  if (d.hostname) parts.push(d.hostname);
  if (d.user) parts.push(`사용자 ${d.user}`);
  if (d.screen) parts.push(`${d.screen[0]}×${d.screen[1]}`);
  if (!d.online) parts.push(`오프라인 (${ago(d.last_seen)})`);
  else if (iv) parts.push(`${modeText(d)} · 마지막 캡처 ${ago(d.last_frame_at)}`);
  else parts.push(`${d.fps ?? 0} fps · ${ago(d.last_frame_at)}`);
  $('#sMeta').textContent = parts.join(' · ');
}

function renderStrip() {
  const strip = $('#sStrip');
  const list = sortedDevices();
  const want = new Set(list.map(d => d.id));
  for (const [id, el] of thumbs) if (!want.has(id)) { el.remove(); thumbs.delete(id); }
  list.forEach((d, i) => {
    let el = thumbs.get(d.id);
    if (!el) {
      el = document.createElement('button');
      el.type = 'button';
      el.className = 's-thumb';
      el.innerHTML = '<img alt="" hidden><span class="nm"></span>';
      el.onclick = () => showSlide(d.id);
      const fr = state.frames.get(d.id);
      if (fr) { el.firstChild.src = fr.url; el.firstChild.hidden = false; }
      thumbs.set(d.id, el);
    }
    if (strip.children[i] !== el) strip.insertBefore(el, strip.children[i] || null);
    el.querySelector('.nm').textContent = label(d);
    el.title = `${label(d)} · ${d.ip}` + (d.online ? '' : ' · 오프라인');
    el.classList.toggle('on', d.id === slide.id);
    el.classList.toggle('offline', !d.online);
    el.classList.toggle('skip', !slideOk(d));
  });
}

// 진행 막대 + 자동 넘김 타이머 (일시정지하면 남은 시간을 기억했다가 이어서)
function stopTimer() { clearTimeout(slide.timer); slide.timer = 0; }
function restartTimer() { slide.remain = slide.dwell * 1000; runTimer(); }
function runTimer() {
  stopTimer();
  const bar = $('#sBar');
  const total = slide.dwell * 1000;
  bar.style.transition = 'none';
  bar.style.transform = `scaleX(${Math.min(1, 1 - slide.remain / total)})`;
  updatePlayBtn();
  if (!slide.on || !slide.playing || !slide.id) return;
  void bar.offsetWidth;
  bar.style.transition = `transform ${slide.remain}ms linear`;
  bar.style.transform = 'scaleX(1)';
  slide.startedAt = performance.now();
  slide.timer = setTimeout(onSlideTimer, slide.remain);
}
function onSlideTimer() {
  slide.timer = 0;
  if (state.focused) return restartTimer();  // 확대 보기를 보고 있는 동안에는 넘기지 않는다
  slideGo(1);
}
function setPlaying(p) {
  if (slide.playing && !p && slide.timer) slide.remain = Math.max(0, slide.remain - (performance.now() - slide.startedAt));
  slide.playing = p;
  store('slidePlay', p ? '1' : '0');
  runTimer();
  renderSlide(false);
}
function updatePlayBtn() {
  const b = $('#sPlay');
  const html = slide.playing ? ICON_PAUSE : ICON_PLAY;
  if (b.dataset.html !== html) { b.innerHTML = html; b.dataset.html = html; }
  b.title = slide.playing ? '일시정지 (Space)' : '재생 (Space)';
}

$('#sDwell').replaceChildren(...DWELLS.map(([v, t]) => new Option(t, v)));
$('#sDwell').value = String(slide.dwell);
$('#sDwell').onchange = (e) => { slide.dwell = Number(e.target.value); store('slideDwell', slide.dwell); restartTimer(); };
$('#sSkip').checked = slide.skipOff;
$('#sSkip').onchange = (e) => { slide.skipOff = e.target.checked; store('slideSkipOff', slide.skipOff ? '1' : '0'); slideSync(); };
$('#sHq').checked = slide.hq;
$('#sHq').onchange = (e) => { slide.hq = e.target.checked; store('slideHq', slide.hq ? '1' : '0'); syncFocus(); };
$('#sPrev').onclick = () => slideGo(-1);
$('#sNext').onclick = () => slideGo(1);
$('#sPlay').onclick = () => setPlaying(!slide.playing);
$('#sOpen').onclick = () => { if (slide.id) openViewer(slide.id); };
$('#sFull').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else $('#slides').requestFullscreen().catch(() => {});
};
// 클릭 = 일시정지/재생, 더블클릭 = 전체화면 (더블클릭의 두 번 클릭은 서로 상쇄)
$('#sStage').onclick = () => { if (slide.id) setPlaying(!slide.playing); };
$('#sStage').ondblclick = () => $('#sFull').click();

// 전체화면에서는 마우스를 3초간 움직이지 않으면 조작 막대를 숨긴다
let idleTimer = 0;
function poke() {
  const el = $('#slides');
  el.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (document.fullscreenElement === el) el.classList.add('idle'); }, 3000);
}
$('#slides').addEventListener('mousemove', poke);
document.addEventListener('fullscreenchange', poke);

/* ------------------------------------------------------------------ */
/* PC 관리                                                             */
/* ------------------------------------------------------------------ */
function setPending(list) {
  state.pending = list || [];
  const n = state.pending.length;
  $('#pendingBadge').hidden = n === 0;
  $('#pendingBadge').textContent = n;
  renderManage();
}

function item(dotOn, title, sub, buttons, cls = '') {
  const el = document.createElement('div');
  el.className = 'item ' + cls;
  if (dotOn !== null) {
    const dot = document.createElement('span');
    dot.className = 'dot' + (dotOn ? ' on' : '');
    el.append(dot);
  }
  const main = document.createElement('div');
  main.className = 'main';
  const b = document.createElement('b'); b.textContent = title;
  const s = document.createElement('small'); s.textContent = sub;
  main.append(b, s);
  el.append(main, ...buttons);
  return el;
}
function button(text, cls, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn ' + cls;
  b.textContent = text;
  b.onclick = fn;
  return b;
}

function renderManage() {
  // 등록 대기
  const pl = $('#pendingList');
  pl.replaceChildren(...state.pending.map(p => item(null, p.ip,
    [p.hostname, p.user && `사용자 ${p.user}`, ago(p.last_try)].filter(Boolean).join(' · '),
    [button('등록', 'primary', () => {
      const f = $('#addForm');
      f.ip.value = p.ip;
      f.name.value = p.hostname || '';
      f.name.focus();
      f.name.select();
    })], 'pending')));
  $('#pendingSec').hidden = state.pending.length === 0;
  $('#pendingCount').textContent = state.pending.length;

  // 등록된 PC
  const list = [...state.devices.values()].sort((a, b) => ipKey(a.ip).localeCompare(ipKey(b.ip)));
  $('#regCount').textContent = `(${list.length})`;
  $('#regList').replaceChildren(...(list.length ? list.map(d => {
    const el = item(d.online, `${d.name || '(이름 없음)'} · ${d.ip}`,
      d.online ? [d.hostname, d.user && `사용자 ${d.user}`, '접속 중'].filter(Boolean).join(' · ')
               : `오프라인 · ${d.last_seen ? '마지막 접속 ' + ago(d.last_seen) : '접속 기록 없음'}`,
      [button('수정', '', () => openEdit(d)), button('삭제', 'danger', () => removeDevice(d))]);
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = d.interval ? `${fmtIv(d.interval)} 주기` : '실시간';
    if (d.client_interval) tag.textContent += ` · PC 저부하 ${fmtIv(d.client_interval)}`;
    el.querySelector('.main small').append(tag);
    return el;
  })
    : [Object.assign(document.createElement('div'), {className: 'hint', textContent: '아직 등록된 PC가 없습니다.'})]));
}

function openManage() {
  $('#drawerOv').classList.add('open');
  loadConnInfo();
  setTimeout(() => $('#addForm').ip.focus(), 50);
}

async function loadConnInfo() {
  try {
    const info = await api('GET', '/api/info');
    // 관리자가 접속한 주소가 localhost 면 실제 네트워크 주소를 먼저 보여준다
    const isLocal = /^(localhost|127\.|\[::1\])/.test(location.hostname);
    const urls = [...new Set(isLocal ? info.server_urls : [location.origin, ...info.server_urls])];
    const rows = [codeRow('서버 주소', urls[0] || location.origin)];
    if (urls.length > 1) {
      const more = document.createElement('div');
      more.className = 'hint';
      more.textContent = '다른 네트워크 주소: ' + urls.slice(1).join(', ');
      rows.push(more);
    }
    rows.push(codeRow('접속 토큰', info.agent_token));
    $('#connInfo').replaceChildren(...rows);
  } catch (e) { /* ignore */ }
}
function codeRow(title, value) {
  const wrap = document.createElement('div');
  const t = document.createElement('div'); t.className = 'hint'; t.textContent = title;
  const c = document.createElement('div'); c.className = 'code';
  const v = document.createElement('span'); v.textContent = value;
  c.append(v, button('복사', '', async () => {
    try { await navigator.clipboard.writeText(value); toast('복사했습니다', 'ok'); }
    catch { toast('복사할 수 없습니다. 직접 선택해 복사하세요', 'warn'); }
  }));
  wrap.append(t, c);
  return wrap;
}

$('#addForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  $('#addErr').textContent = '';
  try {
    const d = await api('POST', '/api/devices',
      {ip: f.ip.value.trim(), name: f.name.value.trim(), interval: Number(f.interval.value)});
    toast(`${label(d)} (${d.ip}) 등록되었습니다`, 'ok');
    const keep = f.interval.value;  // 여러 대를 같은 방식으로 연속 등록하기 쉽도록 전송 방식은 유지
    f.reset();
    f.interval.value = keep;
    f.ip.focus();
  } catch (err) { $('#addErr').textContent = err.message; }
});

function editModeHint() {
  const f = $('#editForm');
  const d = state.devices.get(f.ip.value);
  const iv = Number(f.interval.value);
  const lines = [iv
    ? `PC가 ${fmtIv(iv)}마다 화면을 한 장만 캡처해 보냅니다. 실시간보다 PC 부하와 네트워크 사용량이 크게 줄어듭니다.`
    : '화면 변화가 있을 때 초당 1장까지 보냅니다 (확대 보기 시 초당 8장).'];
  if (iv && d && d.version && d.version.localeCompare('1.1.0', undefined, {numeric: true}) < 0)
    lines.push(`⚠ 이 PC의 클라이언트(v${d.version})는 주기 캡처를 지원하지 않아 계속 실시간으로 전송합니다. 1.1.0 이상으로 업데이트하세요.`);
  if (d && d.client_interval) lines.push(`※ 이 PC는 클라이언트 앱에서 저부하 모드(${fmtIv(d.client_interval)})가 켜져 있어, 둘 중 긴 주기가 적용되고 확대 보기에서도 실시간으로 전환되지 않습니다.`);
  $('#editModeHint').textContent = lines.join(' ');
  f.live_on_focus.disabled = !iv;
  f.live_on_focus.closest('label').style.opacity = iv ? '' : '.5';
}

function openEdit(d) {
  const f = $('#editForm');
  f.ip.value = d.ip;
  f.name.value = d.name || '';
  fillIntervalSelect(f.interval, d.interval || 0);
  f.live_on_focus.checked = d.live_on_focus !== false;
  f.apply_all.checked = false;
  editModeHint();
  $('#editOv').classList.add('open');
  setTimeout(() => f.name.focus(), 50);
}
$('#editForm').interval.addEventListener('change', editModeHint);
$('#editForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {ip: f.ip.value, name: f.name.value.trim(), interval: Number(f.interval.value),
                live_on_focus: f.live_on_focus.checked, apply_all: f.apply_all.checked};
  if (body.apply_all && !confirm(`등록된 모든 PC(${state.devices.size}대)의 전송 방식을 '${f.interval.selectedOptions[0].text}'(으)로 변경할까요?`)) return;
  try {
    await api('POST', '/api/devices', body);
    $('#editOv').classList.remove('open');
    toast(body.apply_all ? '모든 PC의 전송 방식을 변경했습니다' : '저장했습니다', 'ok');
  } catch (err) { toast(err.message, 'bad'); }
});

async function removeDevice(d) {
  if (!confirm(`${label(d)} (${d.ip}) 를 삭제할까요?\n삭제하면 이 PC의 화면 수집이 중단됩니다.`)) return;
  try { await api('DELETE', `/api/devices/${encodeURIComponent(d.ip)}`); toast(`${d.ip} 삭제되었습니다`); }
  catch (err) { toast(err.message, 'bad'); }
}

/* ------------------------------------------------------------------ */
/* 기타 컨트롤                                                          */
/* ------------------------------------------------------------------ */
document.querySelectorAll('.overlay').forEach(ov => {
  ov.addEventListener('click', (e) => {
    if (e.target === ov || e.target.closest('[data-close]')) ov.classList.remove('open');
  });
});

function syncSeg() {
  document.querySelectorAll('#viewSeg button').forEach(b => b.classList.toggle('on', (b.dataset.view === 'slide') === slide.on));
  document.querySelectorAll('#colsSeg button').forEach(b => b.classList.toggle('on', Number(b.dataset.cols) === state.cols));
  document.querySelectorAll('#fitSeg button').forEach(b => b.classList.toggle('on', b.dataset.fit === state.fit));
}
$('#colsSeg').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.cols = Number(b.dataset.cols); store('cols', state.cols); syncSeg(); renderWall();
};
$('#fitSeg').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.fit = b.dataset.fit; store('fit', state.fit); syncSeg(); renderWall();
};
$('#viewSeg').onclick = (e) => {
  const b = e.target.closest('button'); if (b) setView(b.dataset.view);
};
$('#search').oninput = (e) => { state.filter = e.target.value; renderWall(); slideSync(); };
$('#btnManage').onclick = openManage;
$('#btnEmptyAdd').onclick = openManage;
$('#btnLogout').onclick = async () => { await api('POST', '/api/logout').catch(() => {}); location.href = '/login'; };
$('#btnPw').onclick = () => { $('#pwForm').reset(); $('#pwErr').textContent = ''; $('#pwOv').classList.add('open'); };
$('#pwForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new.value !== f.confirm.value) { $('#pwErr').textContent = '새 비밀번호가 일치하지 않습니다'; return; }
  try {
    await api('POST', '/api/password', {current: f.current.value, new: f.new.value});
    $('#pwOv').classList.remove('open');
    toast('비밀번호가 변경되었습니다', 'ok');
  } catch (err) { $('#pwErr').textContent = err.message; }
});

// 표시용 상대 시간 주기적 갱신
setInterval(() => {
  for (const id of state.tiles.keys()) updateTile(id);
  if (slide.on) renderSlide(false);
  if ($('#drawerOv').classList.contains('open')) renderManage();
}, 5000);

let resizeTimer = 0;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => layoutWall(sortedDevices().length), 100);
});

fillIntervalSelect($('#addForm').interval, 0);
syncSeg();
// 주소 끝에 #slide / #grid 를 붙이면 그 보기로 시작 (상황실 모니터 등 바로가기용)
setView(location.hash === '#slide' ? 'slide' : location.hash === '#grid' ? 'grid' : store('view') || 'grid');
connect();
