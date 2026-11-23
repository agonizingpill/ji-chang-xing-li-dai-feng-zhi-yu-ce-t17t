const token = () => localStorage.getItem('airport_token');
const endpoint = '/api/mobile-dispatch';

function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path = '', body, method) {
  const response = await fetch(endpoint + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { 'Authorization': `Bearer ${token() || ''}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}

async function wavesMap() {
  const r = await fetch('/api/waves', { headers: { Authorization: `Bearer ${token() || ''}` } });
  const rows = r.ok ? await r.json() : [];
  return new Map(rows.map(w => [w.id, w]));
}

const role = () => JSON.parse(localStorage.getItem('airport_user') || '{}').role;

const launcher = el('button', '机动班组调度');
launcher.type = 'button';
launcher.id = 'mobile-dispatch-open';
launcher.hidden = true;
const dialog = el('dialog');
dialog.id = 'mobile-dispatch';
const header = el('header');
const title = el('h2', '机动班组 · 撞峰缺口调度');
const closeBtn = el('button', '关闭');
closeBtn.addEventListener('click', () => dialog.close());
header.append(title, closeBtn);

const controls = el('section');
controls.className = 'md-controls';
const terminalSelect = el('select');
terminalSelect.setAttribute('aria-label', '航站楼筛选');
const refreshBtn = el('button', '刷新看板');
const recomputeBtn = el('button', '按最新预测重排');
recomputeBtn.className = 'md-primary';
const registerBtn = el('button', '+ 登记机动班组');
controls.append(terminalSelect, refreshBtn, recomputeBtn, registerBtn);

const statusLine = el('p');
statusLine.className = 'md-status';
statusLine.setAttribute('role', 'status');
const teamsBox = el('section');
const boardBox = el('section');
dialog.append(header, controls, statusLine, teamsBox, boardBox);
document.body.append(launcher, dialog);

function showLaunch() {
  try { launcher.hidden = !token(); } catch { launcher.hidden = true; }
}
showLaunch();
window.addEventListener('storage', showLaunch);
setInterval(showLaunch, 600);

const slotLabel = v => new Date(v).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
const num = v => (v === null || v === undefined ? '—' : Number(v).toLocaleString('zh-CN'));

function renderTeams(teams) {
  teamsBox.replaceChildren();
  const head = el('h3', '机动班组台账');
  teamsBox.append(head);
  if (!teams.length) {
    teamsBox.append(el('p', '当前航站楼还没有机动班组，请先登记。'));
    return;
  }
  const table = el('table');
  table.innerHTML = '<thead><tr><th>班组编号</th><th>航站楼</th><th>单档保障量(件)</th><th>状态</th></tr></thead>';
  const tbody = el('tbody');
  for (const t of teams) {
    const tr = el('tr');
    tr.innerHTML = `<td class="mono">${t.team_code}</td><td>${t.terminal}</td><td>${num(t.capacity)}</td><td>${t.active ? '在岗' : '停用'}</td>`;
    tbody.append(tr);
  }
  table.append(tbody);
  teamsBox.append(table);
}

function renderBoard(slots, waveNames) {
  boardBox.replaceChildren();
  const head = el('h3', '每十分钟档缺口与派工');
  boardBox.append(head);
  if (!slots.length) {
    boardBox.append(el('p', '当前没有就绪预测或没有可排的十分钟档。'));
    return;
  }
  const groups = new Map();
  for (const row of slots) {
    const key = `${row.terminal} · ${slotLabel(row.slot_start)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  for (const [key, rows] of groups) {
    const card = el('div');
    card.className = 'md-slot';
    const cardHead = el('div');
    cardHead.className = 'md-slot-head';
    cardHead.append(el('b', key));
    card.append(cardHead);
    for (const row of rows) {
      const wave = waveNames.get(row.wave_id);
      const line = el('div');
      line.className = 'md-wave-row';
      const info = el('div');
      const name = `${wave ? wave.code : ('波次#' + row.wave_id)}`;
      info.innerHTML = `<b class="mono">${name}</b>
        <span>预测 <b>${num(row.predicted_bags)}</b> 件 · 已确认保障 <b>${num(row.confirmed_coverage)}</b> 件 · 缺口 <b class="${row.gap > 0 ? 'md-gap' : 'md-ok'}">${num(row.gap)}</b> 件</span>`;
      const chipBox = el('div');
      chipBox.className = 'md-chips';
      if (!row.allocations.length) {
        chipBox.append(el('em', row.gap > 0 ? '无可用机动班组' : '缺口已覆盖'));
      }
      for (const a of row.allocations) {
        const chip = el('span');
        chip.className = 'md-chip ' + (a.status === 'confirmed' ? 'confirmed' : 'pending');
        chip.append(el('b', a.team_code));
        chip.append(el('small', `${num(a.allocated_bags)} 件 · ${a.status === 'confirmed' ? '已确认' : '待确认'}`));
        if (a.status === 'pending' && role() === 'supervisor') {
          const btn = el('button', '主管确认');
          btn.addEventListener('click', () => confirmOne(a.id));
          chip.append(btn);
        }
        chipBox.append(chip);
      }
      line.append(info, chipBox);
      card.append(line);
    }
    boardBox.append(card);
  }
}

async function load() {
  statusLine.textContent = '正在加载调度看板…';
  try {
    const terminal = terminalSelect.value || '';
    const [board, waveNames] = await Promise.all([
      request('/board' + (terminal ? `?terminal=${encodeURIComponent(terminal)}` : '')),
      wavesMap(),
    ]);
    const current = terminalSelect.value;
    terminalSelect.replaceChildren(...board.terminals.map(t => new Option(t, t)));
    if (current) terminalSelect.value = current;
    renderTeams(board.teams);
    renderBoard(board.slots, waveNames);
    const pending = board.slots.reduce((n, s) => n + s.allocations.filter(a => a.status === 'pending').length, 0);
    statusLine.textContent = `生成于 ${new Date(board.generated_at).toLocaleTimeString('zh-CN')} · 待确认派工 ${pending} 条`;
  } catch (error) {
    statusLine.textContent = error.message;
  }
}

async function recompute() {
  const terminal = terminalSelect.value;
  if (!terminal) { statusLine.textContent = '请先选择航站楼再重排'; return; }
  recomputeBtn.disabled = true;
  try {
    const result = await request('/recompute', { terminal });
    statusLine.textContent = `已按最新预测重排 ${result.allocations.length} 条派工（版本 v${result.terminal_version}），旧的待确认安排已失效`;
    await load();
  } catch (error) {
    statusLine.textContent = error.message;
  } finally {
    recomputeBtn.disabled = false;
  }
}

function confirmOne(id) {
  const note = prompt('请填写主管确认说明（至少 3 个字符）：', '确认该机动班组本档派工，归属不再随重排变化。');
  if (note === null) return;
  request(`/allocations/${id}/confirm`, { note })
    .then(() => { statusLine.textContent = '派工已确认，归属保留'; return load(); })
    .catch(error => { statusLine.textContent = error.message; });
}

function showRegister() {
  const code = prompt('机动班组编号：', 'FLEX-T2-D');
  if (!code) return;
  const terminal = prompt('所属航站楼：', terminalSelect.value || 'T2');
  if (!terminal) return;
  const capacity = Number(prompt('单档可保障行李件数：', '40'));
  if (!Number.isFinite(capacity) || capacity <= 0) { statusLine.textContent = '保障量必须是正整数'; return; }
  request('/teams', { team_code: code, terminal, capacity })
    .then(() => { statusLine.textContent = `班组 ${code} 已登记`; return load(); })
    .catch(error => { statusLine.textContent = error.message; });
}

terminalSelect.addEventListener('change', load);
refreshBtn.addEventListener('click', load);
recomputeBtn.addEventListener('click', recompute);
registerBtn.addEventListener('click', showRegister);
launcher.addEventListener('click', () => { dialog.showModal(); load(); });
