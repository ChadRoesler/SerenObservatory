// ── SerenObservatory glance - leaf logic on the SerenMeninges shell ──────────
// The shell provides api() (same-origin, auto-attaches the saved bearer),
// escapeHtml(), showTab(), getToken(), and the 🔑 token modal. We call them.
//
// Auth shape (deliberately different from the rest of the family): the
// Observatory API is NOT public (only ping/version are), and its token is a
// SECRETS-FILE interlock, not a config field. So:
//   • node has NO token provisioned -> safe GETs are open -> the glance loads
//     read-only, but lifecycle ACTIONS fail closed (503) until secrets.json.
//   • node HAS a token -> GETs need it too -> punch it into 🔑 Token; a 401
//     on a read means "enter your observatory token."
// We surface both states instead of hiding them.

const $ = (id) => document.getElementById(id);
const API = '/api/v1';
let _noToken = false;   // node appears unprovisioned -> mutations will 503

function statusOf(e) {
    const m = /(\d{3})/.exec((e && e.message) || '');
    return m ? parseInt(m[1], 10) : 0;
}
function showError(html) { $('error-slot').innerHTML = `<div class="err">${html}</div>`; }
function clearError() { $('error-slot').innerHTML = ''; }

function tokenHint(code) {
    if (code === 401) return `⚠ <b>Token required.</b> This node has auth on - set your observatory token via <b>🔑 Token</b> to see live data.`;
    if (code === 503) return `⚠ <b>No token provisioned on this node.</b> The read-only glance works; lifecycle actions stay disabled until <code>~/.seren/secrets.json</code> exists (run <code>seren-secrets.sh</code>).`;
    return '';
}

// -- formatters ----------------------------------------------------------
function fmtUptime(s) {
    if (s == null) return '-';
    s = Math.floor(s);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
}
function tempClass(c) { return c == null ? '' : (c >= 80 ? 'hot' : (c >= 65 ? 'warm' : '')); }
function statTemp(c) { return c == null ? '' : (c >= 80 ? 'hot' : (c >= 65 ? 'warn' : 'ok')); }
function statMem(p) { return p == null ? '' : (p >= 90 ? 'hot' : (p >= 75 ? 'warn' : 'ok')); }

// -- header pills --------------------------------------------------------
function renderHealthPill(health) {
    const pill = $('health-pill');
    if (!health) { pill.textContent = 'health: -'; pill.className = 'head-pill'; return; }
    const ok = !!health.ok;
    const idle = (health.idle || []).length;
    pill.textContent = `health: ${ok ? '' : 'degraded '}${health.healthy}/${health.total}${idle ? ` · ${idle} idle` : ''}`;
    pill.title = idle ? `${idle} orchestrated service(s) off until asked for: ${(health.idle || []).join(', ')}` : 'local service health';
    // The accent while everything that should be up is up; bright red when
    // it is not (20 Oct 2026, third pass: "head pill should be in
    // accent color").
    pill.className = 'head-pill ' + (ok ? 'hot' : 'bad');
    pill.style.display = '';
}

// -- system panel --------------------------------------------------------
function renderVitals(node, thermal, health) {
    const rt = (node && node.runtime) || {};
    const maxT = thermal && thermal.available ? thermal.max_temp_c : null;
    const memP = rt.memory_pct_used;
    // The percent alone hides the size of the box: 60% of a Nano is not 60% of
    // the Spark. Say used/total beside it when the node reports both.
    const gb = mb => (mb / 1024).toFixed(1);
    const memLbl = (rt.memory_mb_total && rt.memory_mb_available != null)
        ? `mem ${gb(rt.memory_mb_total - rt.memory_mb_available)}/${gb(rt.memory_mb_total)} GB`
        : 'mem used';
    const idleN = health && Array.isArray(health.idle) ? health.idle.length : 0;
    const load1 = Array.isArray(rt.load_avg) ? rt.load_avg[0] : null;
    const stat = (cls, big, lbl) => `<div class="stat ${cls}"><div class="big">${big}</div><div class="lbl">${lbl}</div></div>`;
    $('vitals').innerHTML = [
        stat(statTemp(maxT), maxT != null ? `${maxT}°` : '-', 'max temp'),
        stat(statMem(memP), memP != null ? `${memP}%` : '-', memLbl),
        stat('', load1 != null ? load1.toFixed(2) : '-', 'load 1m'),
        stat('', fmtUptime(rt.uptime_seconds), 'uptime'),
        stat(health ? (health.ok ? 'ok' : 'hot') : '', health ? `${health.healthy}/${health.total}` : '-', idleN ? `healthy, ${idleN} idle` : 'healthy'),
    ].join('');
}

function renderNodeMeta(node) {
    const m = (node && node.manifest) || null;
    const rt = (node && node.runtime) || {};
    const box = $('node-meta');
    if (!m) { box.innerHTML = `<div class="hint">no ~/.seren/node.json on this node</div>`; return; }
    // ip_addresses is a list and was skipped with the other objects, so the
    // panel never said the one thing you look here for (20 Oct 2026). A list
    // of scalars reads as one line.
    const scalar = (v) => v != null && (typeof v !== 'object' || (Array.isArray(v) && v.every(x => typeof x !== 'object')));
    const show = (v) => Array.isArray(v) ? (v.length ? v.join(', ') : '-') : v;
    const named = ['hostname', 'host', 'ip_addresses', 'ip', 'role', 'model', 'board', 'platform'];
    const rows = [];
    for (const k of named) if (scalar(m[k])) rows.push([k, show(m[k])]);
    for (const [k, v] of Object.entries(m)) {
        if (named.includes(k)) continue;
        if (scalar(v)) rows.push([k, show(v)]);
    }
    if (rt.memory_mb_total) rows.push(['memory', `${rt.memory_mb_available} / ${rt.memory_mb_total} MB free`]);
    box.innerHTML = rows.map(([k, v]) =>
        `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(String(v))}</span></div>`).join('')
        || `<div class="hint">node.json present but empty</div>`;
}

function renderThermal(thermal) {
    const box = $('thermal-body');
    if (!thermal || !thermal.available || !(thermal.zones || []).length) {
        box.innerHTML = `<div class="hint">no thermal interface on this node</div>`;
        return;
    }
    box.innerHTML = thermal.zones.map(z => {
        const c = z.temp_c;
        const pct = Math.max(0, Math.min(100, c));
        return `<div class="zone ${tempClass(c)}">
            <span class="zt">${escapeHtml(z.type)}</span>
            <span class="zbar"><span style="width:${pct}%"></span></span>
            <span class="zv">${c}°</span>
        </div>`;
    }).join('');
}

// The .kbh clips on the box (24 Oct 2026: "Observatory should be aware of
// what carabiners are installed on the box"). They ride on /system/node;
// an Observatory from before says nothing, which is not the same as none.
function renderCarabiners(node) {
    const box = $('clips-body');
    if (!node) { box.innerHTML = `<div class="hint">-</div>`; return; }
    if (!Array.isArray(node.carabiners)) {
        box.innerHTML = `<div class="hint">this Observatory does not report carabiners</div>`;
        return;
    }
    if (!node.carabiners.length) {
        box.innerHTML = `<div class="hint">no carabiners clipped on this node</div>`;
        return;
    }
    box.innerHTML = node.carabiners.map(c => {
        const inst = c.install || {};
        const chips = [];
        if (!c.present) chips.push(`<span class="health-chip down" title="the install ledger says it was put here; the .kbh is not there now">missing</span>`);
        else if (!c.configured) chips.push(`<span class="health-chip na" title="no ${escapeHtml(c.carabiner)}.yaml beside the .kbh">no yaml</span>`);
        if (c.rack) chips.push(`<span class="badge orch" title="in carabiners.folder: Lodestar's Rack can read and change its settings">on the Rack</span>`);
        const said = [];
        if (inst.installed_at) said.push(`installed ${inst.installed_at.slice(0, 10)}`);
        if (inst.source) said.push(`from ${inst.source}${inst.source_ref ? ' ' + inst.source_ref : ''}`);
        if (inst.instance) said.push(`instance ${inst.instance}`);
        if (inst.project) said.push(`works in ${inst.project}`);
        return `<div class="clip">
            <span class="dot ${c.present ? 'run' : 'bad'}"></span>
            <span class="nm">${escapeHtml(c.carabiner)}</span>
            <span class="badge" title="${escapeHtml(c.sha256 ? 'sha256 ' + c.sha256 : 'no version read from the file')}">${escapeHtml(c.version || '?')}</span>
            ${chips.join('')}
            <div class="where">${escapeHtml(c.kbh)}${said.length ? ' · ' + escapeHtml(said.join(' · ')) : ''}</div>
        </div>`;
    }).join('');
}

// -- services panel ------------------------------------------------------
function svcRow(name, entry) {
    const st = entry.status || {};
    const m = entry.manifest || {};
    const stype = st.service_type || m.service_type || 'pid_file';
    const lib = st.library_mode || stype === 'library';
    const running = !!st.running;
    const ph = st.port_health;
    // Orchestrated: Lodestar starts it when someone needs it and stops it when
    // nobody does. Off is its resting state - shown as idle, never as down.
    const orch = !!(st.orchestrated || m.orchestrated);
    const idle = orch && !running;

    let healthChip = `<span class="health-chip na">-</span>`;
    if (lib) healthChip = `<span class="health-chip na">n/a</span>`;
    else if (idle) healthChip = `<span class="health-chip idle" title="orchestrated: started on demand, off until asked for">idle</span>`;
    else if (ph && ph.ok) healthChip = `<span class="health-chip up">up${ph.latency_ms != null ? ' ' + ph.latency_ms + 'ms' : ''}</span>`;
    else if (running && ph && !ph.ok) healthChip = `<span class="health-chip down">down</span>`;
    // "On demand" is its own column (20 Oct 2026): a chip there, and
    // nothing riding on the type badge any more.
    const orchCell = orch ? `<span class="badge orch" title="orchestrated: Lodestar starts it when someone needs it and stops it when nobody does">on demand</span>`
                          : `<span class="col-na">-</span>`;

    const dotCls = lib ? 'stop' : (running ? (ph && !ph.ok ? 'bad' : 'run') : (idle ? 'idle' : 'stop'));
    const typeCls = stype === 'library' ? 'lib' : (stype === 'systemd' ? 'systemd' : (stype === 'docker_compose' ? 'docker' : 'pid'));

    const acts = lib ? '' : `
        <button class="btn-mini" onclick="svcAction('${escapeHtml(name)}','start')" ${running ? 'disabled' : ''} title="start">▶</button>
        <button class="btn-mini danger" onclick="svcAction('${escapeHtml(name)}','stop')" ${running ? '' : 'disabled'} title="stop">■</button>
        <button class="btn-mini" onclick="svcAction('${escapeHtml(name)}','restart')" title="restart">⟳</button>`;

    return `<div class="svc-row">
        <span class="dot ${dotCls}" title="${running ? 'running' : (idle ? 'idle (orchestrated)' : 'stopped')}"></span>
        <span class="nm">${escapeHtml(name)}</span>
        <span class="col"><span class="badge ${typeCls}">${escapeHtml(stype)}</span></span>
        <span class="col">${orchCell}</span>
        <span class="col">${healthChip}</span>
        <span class="col">${st.memory_mb != null ? st.memory_mb + ' MB' : '-'}</span>
        <span class="col">${st.cpu_percent != null ? st.cpu_percent + '%' : '-'}</span>
        <span class="col">${fmtUptime(st.uptime_seconds)}</span>
        <span class="acts">${acts}<button class="btn-mini" onclick="svcLogs('${escapeHtml(name)}')" title="logs">📄</button></span>
    </div>`;
}

function renderServices(data) {
    const services = (data && data.services) || {};
    const names = Object.keys(services).sort();
    if (!names.length) { $('svc-table').innerHTML = `<div class="empty">no services installed on this node</div>`; return; }
    const head = `<div class="svc-head">
        <span></span><span>service</span><span>type</span><span>on demand</span><span>health</span>
        <span>mem</span><span>cpu</span><span>uptime</span><span style="text-align:right;">actions</span>
    </div>`;
    $('svc-table').innerHTML = head + names.map(n => svcRow(n, services[n])).join('');
}

async function svcAction(name, action) {
    try {
        clearError();
        await api(`${API}/service/${name}/${action}`, { method: 'POST', body: '{}' });
        await loadServices();
    } catch (e) {
        const code = statusOf(e);
        if (code === 503) { _noToken = true; updateAuthNote(); showError(tokenHint(503)); }
        else if (code === 401) showError(tokenHint(401));
        else showError(`<b>${escapeHtml(action)} failed:</b> ${escapeHtml(e.message)}`);
    }
}

async function svcLogs(name) {
    const box = $('logbox');
    box.style.display = 'block';
    box.textContent = `loading ${name} logs…`;
    try {
        const data = await api(`${API}/service/${name}/logs?lines=200`);
        const lines = (data && data.lines) || [];
        box.textContent = lines.length ? lines.join('\n') : `(no log lines for ${name})`;
    } catch (e) {
        box.textContent = statusOf(e) === 401 ? 'Token required - set it via 🔑 Token.' : `failed to load logs: ${e.message}`;
    }
}

function updateAuthNote() {
    const note = $('auth-note');
    if (_noToken) { note.innerHTML = 'actions disabled - no token on this node'; note.className = 'hint warn'; }
    else { note.textContent = 'actions use your 🔑 token'; note.className = 'hint'; }
}

// -- loaders / boot ------------------------------------------------------
async function loadServices() {
    const data = await api(`${API}/system/services`);
    renderServices(data);
    return data;
}

async function boot() {
    clearError();
    const [node, thermal, health, services] = await Promise.allSettled([
        api(`${API}/system/node`),
        api(`${API}/system/thermal`),
        api(`${API}/system/health`),
        api(`${API}/system/services`),
    ]);

    if (node.status === 'rejected' && statusOf(node.reason) === 401) {
        // Every read on a provisioned node needs the token, so a first visit
        // is a blank page unless this is said loudly. Once per visit, open
        // the token modal itself; the banner stays until a token is saved.
        showError(tokenHint(401) + ` <button class="btn-mini" onclick="openToken()">enter token</button>`);
        if (!getToken() && !window._askedToken && typeof openToken === 'function') { window._askedToken = true; openToken(); }
    }

    // Infer provisioning: open GETs with no token entered => the node has no
    // secret => lifecycle mutations will fail closed (503). Surface proactively.
    _noToken = node.status === 'fulfilled' && !getToken();
    updateAuthNote();

    const nodeV = node.status === 'fulfilled' ? node.value : null;
    const thermalV = thermal.status === 'fulfilled' ? thermal.value : null;
    const healthV = health.status === 'fulfilled' ? health.value : null;

    renderHealthPill(healthV);
    renderVitals(nodeV, thermalV, healthV);
    renderNodeMeta(nodeV);
    renderThermal(thermalV);
    renderCarabiners(nodeV);

    if (services.status === 'fulfilled') renderServices(services.value);
    else $('svc-table').innerHTML = `<div class="empty">${statusOf(services.reason) === 401 ? 'enter your token to load services' : 'services unavailable'}</div>`;
}

// Header ⟳ button (header_aside.html) calls this.
function reload() { boot(); }

boot();
