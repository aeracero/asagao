'use strict';

/* 週テンプレートは「2024年1月1日（月）〜7日（日）」の固定の1週間として表示する。
   実際の日付には意味がなく、曜日と時刻だけを使う。 */
const DAYS = ['月', '火', '水', '木', '金', '土', '日'];
const BASE_DATE = '2024-01-01';
const PALETTE = [
  ['#7B3FA0', '#FFFFFF'], ['#2F7D3A', '#FFFFFF'], ['#F2B233', '#2A2100'], ['#5E6878', '#FFFFFF'],
  ['#9A5B2E', '#FFFFFF'], ['#1F8A84', '#FFFFFF'], ['#C93B6E', '#FFFFFF'], ['#3A6FD8', '#FFFFFF'],
];
const NO_VC_COLOR = ['#9AA0B8', '#FFFFFF'];
// 時間ごとの空の色（時刻, 色）
const SKY = [
  [0, '#131938'], [4, '#1E2A5C'], [5, '#4B3F7A'], [5.5, '#9A5F8F'], [6, '#E58F86'],
  [6.5, '#F5C27A'], [7.5, '#BFE0F2'], [12, '#D2EAF8'], [16, '#BCDDF1'], [17.5, '#F2B880'],
  [18.5, '#B5627A'], [19.5, '#3D3772'], [21, '#1C2350'], [24, '#131938'],
];

const state = {
  me: null, guilds: [], channelInfo: new Map(), alarms: new Map(), sounds: [], others: [],
  levels: [20, 40, 60, 80, 100], selected: null, mobile: false, day: 0,
};
let calendar = null;
let pollTimer = null;
let previewAudio = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --- API ------------------------------------------------------------------
async function api(method, path, body) {
  const opt = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch { /* 空の応答 */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `うまくいきませんでした（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), bad ? 4500 : 2200);
}

// --- 時刻の変換 -------------------------------------------------------------
const dateFor = (weekday, minutes) => new Date(2024, 0, 1 + weekday, 0, minutes);
const weekdayOf = (date) => date.getDate() - 1;
function minutesOf(date, weekday) {
  return (weekdayOf(date) - weekday) * 1440 + date.getHours() * 60 + date.getMinutes();
}
const pad = (n) => String(n).padStart(2, '0');
const hhmm = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
function parseHHMM(value) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(value || '');
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
function withinOneDay(start, end) {
  const dayStart = new Date(start);
  dayStart.setHours(0, 0, 0, 0);
  return end - dayStart <= 86400000;
}
function fmtDuration(sec) {
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

// --- 色 -----------------------------------------------------------------------
function colorFor(channelId) {
  if (!channelId) return NO_VC_COLOR;
  let h = 0;
  for (const ch of channelId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function skyColor(hour) {
  for (let i = 1; i < SKY.length; i++) {
    const [h1, c1] = SKY[i];
    if (hour <= h1) {
      const [h0, c0] = SKY[i - 1];
      return mix(c0, c1, (hour - h0) / (h1 - h0));
    }
  }
  return SKY[SKY.length - 1][1];
}
function mix(a, b, t) {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return '#' + pa.map((v, i) => pad(Math.round(v + (pb[i] - v) * t).toString(16))).join('');
}
function isDark(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.55;
}
function paintSky() {
  for (const td of $$('#calendar td.fc-timegrid-slot-label')) {
    const [h, m] = (td.dataset.time || '0:0').split(':').map(Number);
    const color = skyColor(h + m / 60 + 0.25);
    td.style.background = color;
    td.classList.toggle('night', isDark(color));
    td.classList.toggle('day', !isDark(color));
  }
}

// --- 重なりチェック --------------------------------------------------------
function conflicts() {
  const items = [];
  for (const a of state.alarms.values()) {
    if (!a.enabled || !a.channel_id) continue;
    items.push({ id: a.id, wd: a.weekday, s: a.start_min - a.join_offset / 60, e: a.end_min, g: a.guild_id, c: a.channel_id });
  }
  for (const o of state.others) {
    items.push({ id: null, wd: o.weekday, s: o.start_min - o.join_offset / 60, e: o.end_min, g: o.guild_id, c: o.channel_id });
  }
  const bad = new Set();
  for (const a of items) {
    if (a.id === null) continue;
    for (const b of items) {
      if (a === b || a.wd !== b.wd || a.g !== b.g || a.c === b.c) continue;
      if (a.s < b.e && b.s < a.e) bad.add(a.id);
    }
  }
  return bad;
}

// --- カレンダー --------------------------------------------------------------
function buildCalendar() {
  state.mobile = window.matchMedia('(max-width: 760px)').matches;
  calendar = new FullCalendar.Calendar($('#calendar'), {
    initialView: state.mobile ? 'timeGridDay' : 'timeGridWeek',
    initialDate: BASE_DATE,
    validRange: { start: '2024-01-01', end: '2024-01-08' },
    firstDay: 1,
    headerToolbar: false,
    allDaySlot: false,
    nowIndicator: false,
    height: '100%',
    slotDuration: '00:30:00',
    snapDuration: '00:05:00',
    slotLabelInterval: '01:00',
    slotLabelFormat: { hour: '2-digit', minute: '2-digit', hour12: false },
    scrollTime: '05:00:00',
    slotEventOverlap: false,
    eventMinHeight: 26,
    selectable: true,
    selectMirror: true,
    editable: true,
    eventDurationEditable: true,
    eventResizableFromStart: false,
    longPressDelay: 350,
    dayHeaderContent: (arg) => DAYS[weekdayOf(arg.date)],
    dayHeaderClassNames: (arg) => {
      const wd = weekdayOf(arg.date);
      return wd === 5 ? ['sat'] : wd === 6 ? ['sun'] : [];
    },
    selectAllow: (info) => withinOneDay(info.start, info.end),
    eventAllow: (info) => withinOneDay(info.start, info.end),
    select: onSelect,
    eventClick: onEventClick,
    eventDrop: onEventChange,
    eventResize: onEventChange,
    eventContent: renderEventContent,
    viewDidMount: () => requestAnimationFrame(paintSky),
    datesSet: () => requestAnimationFrame(paintSky),
  });
  calendar.render();
  requestAnimationFrame(paintSky);

  if (state.mobile) buildDayTabs();
}

function buildDayTabs() {
  const nav = $('#daytabs');
  nav.hidden = false;
  nav.replaceChildren(...DAYS.map((d, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = d;
    b.setAttribute('aria-pressed', String(i === state.day));
    b.addEventListener('click', () => {
      state.day = i;
      calendar.gotoDate(dateFor(i, 0));
      $$('button', nav).forEach((x, j) => x.setAttribute('aria-pressed', String(j === i)));
    });
    return b;
  }));
}

function renderEvents() {
  if (!calendar) return;
  const bad = conflicts();
  calendar.batchRendering(() => {
    calendar.removeAllEvents();
    for (const a of state.alarms.values()) {
      const [bg, fg] = colorFor(a.channel_id);
      calendar.addEvent({
        id: String(a.id),
        start: dateFor(a.weekday, a.start_min),
        end: dateFor(a.weekday, a.end_min),
        backgroundColor: bg, borderColor: bg, textColor: fg,
        classNames: ['mine', !a.enabled && 'off', bad.has(a.id) && 'conflict', state.selected === a.id && 'selected'].filter(Boolean),
        extendedProps: { kind: 'mine', alarmId: a.id },
      });
    }
    state.others.forEach((o, i) => {
      calendar.addEvent({
        id: `other-${i}`,
        start: dateFor(o.weekday, o.start_min),
        end: dateFor(o.weekday, o.end_min),
        editable: false, startEditable: false, durationEditable: false,
        classNames: ['other'],
        extendedProps: { kind: 'other' },
      });
    });
  });
  renderLegend();
}

function renderEventContent(arg) {
  const p = arg.event.extendedProps;
  const wrap = document.createElement('div');
  wrap.className = 'ev';
  const line = (cls, text) => {
    const d = document.createElement('div');
    d.className = cls;
    d.textContent = text;
    wrap.append(d);
  };
  const start = arg.event.start;
  const end = arg.event.end;
  const wd = weekdayOf(start);
  line('ev-time', `${hhmm(minutesOf(start, wd))}–${hhmm(minutesOf(end, wd))}`);
  if (p.kind === 'other') {
    line('ev-sub', '他の人のアラーム');
    return { domNodes: [wrap] };
  }
  const a = state.alarms.get(p.alarmId);
  if (!a) return { domNodes: [wrap] };
  const vc = a.channel_id ? (state.channelInfo.get(a.channel_id)?.name || '使えないVC') : 'VC未設定';
  line('ev-sub', a.label || `🔊 ${vc}`);
  const meta = document.createElement('div');
  meta.className = 'ev-meta';
  const parts = [
    a.sound_ids.length ? `♪ ${a.sound_ids.length}` : '♪ ビープ',
    a.play_count === 0 ? '止めるまで' : `${a.play_count}回`,
  ];
  if (a.snooze_count) parts.push(`💤 ${a.snooze_count}`);
  meta.append(...parts.map((t) => Object.assign(document.createElement('span'), { textContent: t })));
  wrap.append(meta);
  return { domNodes: [wrap] };
}

function renderLegend() {
  const used = new Map();
  for (const a of state.alarms.values()) {
    if (a.channel_id && !used.has(a.channel_id)) used.set(a.channel_id, state.channelInfo.get(a.channel_id));
  }
  const legend = $('#legend');
  legend.replaceChildren(...[...used].map(([id, info]) => {
    const span = document.createElement('span');
    const sw = document.createElement('i');
    sw.style.background = colorFor(id)[0];
    span.append(sw, info ? `${info.name}（${info.guild}）` : '使えないVC');
    return span;
  }));
}

// --- カレンダー操作 ----------------------------------------------------------
async function onSelect(info) {
  calendar.unselect();
  const wd = weekdayOf(info.start);
  try {
    const { alarm } = await api('POST', '/api/alarms', {
      weekday: wd, start_min: minutesOf(info.start, wd), end_min: minutesOf(info.end, wd),
    });
    state.alarms.set(alarm.id, alarm);
    openPanel(alarm.id);
    toast('アラームを置きました');
  } catch (e) {
    toast(e.message, true);
  }
}

function onEventClick(info) {
  if (info.event.extendedProps.kind === 'mine') openPanel(info.event.extendedProps.alarmId);
}

async function onEventChange(info) {
  const p = info.event.extendedProps;
  if (p.kind !== 'mine') return info.revert();
  const wd = weekdayOf(info.event.start);
  const patch = {
    weekday: wd, start_min: minutesOf(info.event.start, wd), end_min: minutesOf(info.event.end, wd),
  };
  try {
    const { alarms } = await api('PATCH', `/api/alarms/${p.alarmId}`, patch);
    alarms.forEach((a) => state.alarms.set(a.id, a));
    renderEvents();
    if (state.selected === p.alarmId) fillForm(state.alarms.get(p.alarmId));
  } catch (e) {
    info.revert();
    toast(e.message, true);
  }
}

// --- 設定パネル --------------------------------------------------------------
function groupOf(a) {
  if (!a.group_id) return [a];
  return [...state.alarms.values()].filter((x) => x.group_id === a.group_id);
}

function openPanel(id) {
  state.selected = id;
  const a = state.alarms.get(id);
  if (!a) return;
  fillForm(a);
  $('#panel').hidden = false;
  renderEvents();
  if (!state.mobile) calendar.updateSize();
}

function closePanel() {
  state.selected = null;
  $('#panel').hidden = true;
  renderEvents();
  if (!state.mobile) calendar.updateSize();
}

function fillForm(a) {
  const f = $('#alarm-form');
  f.label.value = a.label;
  f.enabled.checked = a.enabled;

  // VC の選択肢（サーバーごと）
  const select = f.channel_id;
  select.replaceChildren(new Option('VCを選んでください', ''));
  for (const g of state.guilds) {
    const og = document.createElement('optgroup');
    og.label = g.name;
    g.channels.forEach((c) => og.append(new Option(c.name, c.id)));
    select.append(og);
  }
  if (a.channel_id && !state.channelInfo.has(a.channel_id)) {
    select.append(new Option('（いまは使えないVC）', a.channel_id));
  }
  select.value = a.channel_id || '';

  f.start.value = hhmm(a.start_min);
  f.end.value = a.end_min === 1440 ? '00:00' : hhmm(a.end_min);

  renderSoundChecks(a.sound_ids);
  f.querySelector(`input[name="sound_mode"][value="${a.sound_mode}"]`).checked = true;

  const seg = $('#volume-seg');
  seg.replaceChildren(...state.levels.map((lv) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'volume';
    input.value = String(lv);
    input.checked = lv === a.volume;
    label.append(input, `${lv}%`);
    return label;
  }));

  for (const key of ['play_count', 'play_interval', 'snooze_count', 'snooze_interval', 'join_offset', 'leave_delay', 'wait_for_user']) {
    f[key].value = a[key];
  }
  for (const key of ['snooze_leave', 'require_user_in_vc', 'leave_on_user_leave']) {
    f[key].checked = a[key];
  }
  f.querySelector(`input[name="stop_mode"][value="${a.stop_mode}"]`).checked = true;

  const group = groupOf(a);
  $('#group-apply').hidden = group.length < 2;
  $('#conflict-note').hidden = !conflicts().has(a.id);
  $('#novc-note').hidden = Boolean(a.channel_id);

  // コピー先の曜日（同じグループですでにある曜日は選べない）
  const taken = new Set(group.map((x) => x.weekday));
  $('#copy-days').replaceChildren(...DAYS.map((d, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = d;
    b.dataset.day = String(i);
    b.disabled = taken.has(i);
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true')));
    return b;
  }));
  resetDeleteButton();
}

function renderSoundChecks(selectedIds) {
  const box = $('#sound-checks');
  if (!state.sounds.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = '音声がまだありません。登録するまではビープ音で鳴ります。';
    box.replaceChildren(p);
    return;
  }
  // 選んだ順（＝「いちばん上の音声」）を保つ
  const order = [...selectedIds, ...state.sounds.map((s) => s.id).filter((id) => !selectedIds.includes(id))];
  box.replaceChildren(...order.map((id) => state.sounds.find((s) => s.id === id)).filter(Boolean).map((s) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'sound';
    input.value = String(s.id);
    input.checked = selectedIds.includes(s.id);
    const name = document.createElement('span');
    name.textContent = s.name;
    const status = document.createElement('span');
    status.className = 'status';
    status.textContent = s.status === 'pending' ? '変換中' : s.status === 'failed' ? '変換失敗' : fmtDuration(s.duration);
    label.append(input, name, status);
    return label;
  }));
}

function readForm() {
  const f = $('#alarm-form');
  const start = parseHHMM(f.start.value);
  let end = parseHHMM(f.end.value);
  if (start === null || end === null) throw new Error('時刻を入力してください。');
  if (end === 0) end = 1440; // 00:00 は「その日の終わり」
  const num = (key) => {
    const v = Number(f[key].value);
    if (!Number.isInteger(v)) throw new Error('数字の欄を確認してください。');
    return v;
  };
  return {
    label: f.label.value,
    enabled: f.enabled.checked,
    channel_id: f.channel_id.value || null,
    start_min: start,
    end_min: end,
    sound_ids: $$('#sound-checks input[name="sound"]:checked').map((i) => Number(i.value)),
    sound_mode: f.querySelector('input[name="sound_mode"]:checked').value,
    volume: Number(f.querySelector('input[name="volume"]:checked')?.value || 60),
    play_count: num('play_count'),
    play_interval: num('play_interval'),
    snooze_count: num('snooze_count'),
    snooze_interval: num('snooze_interval'),
    snooze_leave: f.snooze_leave.checked,
    stop_mode: f.querySelector('input[name="stop_mode"]:checked').value,
    join_offset: num('join_offset'),
    require_user_in_vc: f.require_user_in_vc.checked,
    wait_for_user: num('wait_for_user'),
    leave_on_user_leave: f.leave_on_user_leave.checked,
    leave_delay: num('leave_delay'),
    apply_group: !$('#group-apply').hidden && f.apply_group.checked,
  };
}

async function saveForm(ev) {
  ev.preventDefault();
  const id = state.selected;
  if (!id) return;
  let body;
  try {
    body = readForm();
  } catch (e) {
    return toast(e.message, true);
  }
  const btn = ev.submitter;
  if (btn) btn.disabled = true;
  try {
    const { alarms } = await api('PATCH', `/api/alarms/${id}`, body);
    alarms.forEach((a) => state.alarms.set(a.id, a));
    renderEvents();
    fillForm(state.alarms.get(id));
    toast(alarms.length > 1 ? `${alarms.length}個のアラームを保存しました` : '保存しました');
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function testAlarm() {
  try {
    const r = await api('POST', `/api/alarms/${state.selected}/test`);
    toast(r.message, !r.ok);
  } catch (e) {
    toast(e.message, true);
  }
}

async function copyAlarm() {
  const days = $$('#copy-days button[aria-pressed="true"]').map((b) => Number(b.dataset.day));
  if (!days.length) return toast('コピー先の曜日を選んでください', true);
  try {
    const { alarms } = await api('POST', `/api/alarms/${state.selected}/copy`, { weekdays: days });
    alarms.forEach((a) => state.alarms.set(a.id, a));
    renderEvents();
    fillForm(state.alarms.get(state.selected));
    toast('コピーしました');
  } catch (e) {
    toast(e.message, true);
  }
}

function resetDeleteButton() {
  const b = $('#btn-delete');
  b.classList.remove('armed');
  b.textContent = 'このアラームを削除';
}

async function deleteAlarm() {
  const b = $('#btn-delete');
  if (!b.classList.contains('armed')) {
    b.classList.add('armed');
    b.textContent = 'もう一度押すと削除します';
    setTimeout(resetDeleteButton, 4000);
    return;
  }
  try {
    const { deleted } = await api('DELETE', `/api/alarms/${state.selected}`);
    deleted.forEach((id) => state.alarms.delete(id));
    closePanel();
    toast('削除しました');
  } catch (e) {
    toast(e.message, true);
  }
}

// --- 音声 ----------------------------------------------------------------------
function openSounds() {
  renderSoundList();
  $('#sounds-dialog').showModal();
}

function renderSoundList() {
  const list = $('#sound-list');
  if (!state.sounds.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'まだ音声がありません。上からアップロードしてください。';
    list.replaceChildren(li);
    return;
  }
  list.replaceChildren(...state.sounds.map((s) => {
    const li = document.createElement('li');
    const name = document.createElement('input');
    name.value = s.name;
    name.maxLength = 40;
    name.setAttribute('aria-label', '表示名');
    name.addEventListener('change', () => renameSound(s.id, name.value));
    const meta = document.createElement('span');
    meta.className = `meta ${s.status}`;
    meta.textContent = s.status === 'pending' ? '変換中…' : s.status === 'failed' ? '変換に失敗' : fmtDuration(s.duration);
    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'btn ghost small';
    play.textContent = '試聴';
    play.addEventListener('click', () => preview(s.id, play));
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn danger-link small';
    del.textContent = '削除';
    del.addEventListener('click', () => deleteSound(s.id, del));
    li.append(name, meta, play, del);
    return li;
  }));
}

function preview(id, button) {
  if (previewAudio) {
    previewAudio.pause();
    const wasSame = previewAudio.dataset.id === String(id);
    previewAudio = null;
    $$('#sound-list button').forEach((b) => { if (b.textContent === '停止') b.textContent = '試聴'; });
    if (wasSame) return;
  }
  const f = $('#alarm-form');
  const level = f.querySelector('input[name="volume"]:checked')?.value || 60;
  const audio = new Audio(`/api/sounds/${id}/file?v=${level}`);
  audio.dataset.id = String(id);
  // ogg/opus を再生できないブラウザでは元ファイルで試聴する
  audio.addEventListener('error', () => {
    if (!audio.dataset.fallback) {
      audio.dataset.fallback = '1';
      audio.src = `/api/sounds/${id}/file?original=1`;
      audio.play().catch(() => {});
    }
  });
  audio.addEventListener('ended', () => { button.textContent = '試聴'; previewAudio = null; });
  audio.play().catch(() => {});
  button.textContent = '停止';
  previewAudio = audio;
}

async function renameSound(id, name) {
  try {
    const { sound } = await api('PATCH', `/api/sounds/${id}`, { name });
    replaceSound(sound);
    toast('名前を変えました');
  } catch (e) {
    toast(e.message, true);
  }
}

async function deleteSound(id, button) {
  if (button.dataset.armed !== '1') {
    button.dataset.armed = '1';
    button.textContent = '本当に削除';
    setTimeout(() => { button.dataset.armed = ''; button.textContent = '削除'; }, 4000);
    return;
  }
  try {
    await api('DELETE', `/api/sounds/${id}`);
    state.sounds = state.sounds.filter((s) => s.id !== id);
    for (const a of state.alarms.values()) a.sound_ids = a.sound_ids.filter((x) => x !== id);
    afterSoundsChanged();
    toast('削除しました');
  } catch (e) {
    toast(e.message, true);
  }
}

function replaceSound(sound) {
  const i = state.sounds.findIndex((s) => s.id === sound.id);
  if (i >= 0) state.sounds[i] = sound; else state.sounds.push(sound);
}

function afterSoundsChanged() {
  renderSoundList();
  renderEvents();
  if (state.selected) {
    const checked = $$('#sound-checks input[name="sound"]:checked').map((i) => Number(i.value));
    renderSoundChecks(checked.filter((id) => state.sounds.some((s) => s.id === id)));
  }
  schedulePoll();
}

function uploadSound(ev) {
  ev.preventDefault();
  const form = ev.target;
  const file = form.elements.namedItem('file').files[0];
  const err = $('#upload-error');
  err.hidden = true;
  if (!file) return;
  const data = new FormData();
  data.append('name', form.elements.namedItem('name').value);
  data.append('file', file);
  const bar = $('#upload-progress');
  bar.hidden = false;
  bar.value = 0;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;

  // fetch では送信の進み具合が取れないので XHR を使う
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/sounds');
  xhr.upload.addEventListener('progress', (e) => { if (e.lengthComputable) bar.value = (e.loaded / e.total) * 100; });
  xhr.addEventListener('loadend', () => {
    submit.disabled = false;
    bar.hidden = true;
    let res = null;
    try { res = JSON.parse(xhr.responseText); } catch { /* なし */ }
    if (xhr.status === 200 && res?.sound) {
      replaceSound(res.sound);
      form.reset();
      $('#file-label').textContent = 'ファイルを選ぶ（5MB・3分まで）';
      afterSoundsChanged();
      toast('アップロードしました。変換が終わると使えます');
    } else {
      err.textContent = res?.error || 'アップロードできませんでした。';
      err.hidden = false;
    }
  });
  xhr.send(data);
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (!state.sounds.some((s) => s.status === 'pending')) return;
  pollTimer = setTimeout(async () => {
    try {
      const { sounds } = await api('GET', '/api/sounds');
      state.sounds = sounds;
      afterSoundsChanged();
    } catch { /* 次回また試す */ }
  }, 3000);
}

// --- 起動 ------------------------------------------------------------------------
function showLogin() {
  $('#login').hidden = false;
  const reason = new URLSearchParams(location.search).get('login_error');
  if (reason) {
    const el = $('#login-error');
    el.textContent = reason === 'denied'
      ? 'ログインがキャンセルされました。'
      : 'ログインできませんでした。もう一度試してください。';
    el.hidden = false;
    history.replaceState(null, '', '/');
  }
}

function applyBootstrap(data) {
  state.me = data.me;
  state.guilds = data.guilds;
  state.sounds = data.sounds;
  state.others = data.others;
  state.levels = data.volume_levels;
  state.alarms = new Map(data.alarms.map((a) => [a.id, a]));
  state.channelInfo = new Map();
  for (const g of data.guilds) for (const c of g.channels) state.channelInfo.set(c.id, { name: c.name, guild: g.name });

  $('#me-name').textContent = data.me.name;
  if (data.me.avatar_url) {
    const img = $('#me-avatar');
    img.src = data.me.avatar_url;
    img.hidden = false;
  }
  $('#paused-banner').hidden = !data.me.paused;
}

function bindUi() {
  $('#alarm-form').addEventListener('submit', saveForm);
  $('#panel-close').addEventListener('click', closePanel);
  $('#btn-test').addEventListener('click', testAlarm);
  $('#btn-copy').addEventListener('click', copyAlarm);
  $('#btn-delete').addEventListener('click', deleteAlarm);
  $$('[data-preset]').forEach((b) => b.addEventListener('click', () => {
    const want = b.dataset.preset === 'weekdays' ? [0, 1, 2, 3, 4] : [0, 1, 2, 3, 4, 5, 6];
    $$('#copy-days button').forEach((d) => {
      if (!d.disabled) d.setAttribute('aria-pressed', String(want.includes(Number(d.dataset.day))));
    });
  }));
  $('#btn-sounds').addEventListener('click', openSounds);
  $('#open-sounds-from-panel').addEventListener('click', openSounds);
  $('#sounds-close').addEventListener('click', () => $('#sounds-dialog').close());
  $('#sounds-dialog').addEventListener('close', () => { if (previewAudio) { previewAudio.pause(); previewAudio = null; } });
  $('#upload-form').addEventListener('submit', uploadSound);
  $('#upload-form').elements.namedItem('file').addEventListener('change', (e) => {
    $('#file-label').textContent = e.target.files[0]?.name || 'ファイルを選ぶ（5MB・3分まで）';
  });
  $('#btn-logout').addEventListener('click', async () => {
    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
    location.href = '/';
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#panel').hidden && !$('#sounds-dialog').open) closePanel();
  });
}

async function init() {
  let data;
  try {
    data = await api('GET', '/api/bootstrap');
  } catch (e) {
    if (e.status === 401) return showLogin();
    document.body.textContent = `読み込めませんでした: ${e.message}`;
    return;
  }
  applyBootstrap(data);
  $('#app').hidden = false;
  bindUi();
  buildCalendar();
  renderEvents();
  schedulePoll();
  if (!data.guilds.length) {
    toast('botと同じサーバーが見つかりません。botを招待してから、もう一度ログインしてください。', true);
  }
}

document.addEventListener('DOMContentLoaded', init);
