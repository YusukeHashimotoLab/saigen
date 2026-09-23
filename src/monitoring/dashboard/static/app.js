const deviceContainer = document.getElementById('device-container');
const logs = document.getElementById('logs');
const statusBadge = document.getElementById('connection-status');
const activeCountLabel = document.getElementById('active-count');
const deployTargetSelect = document.getElementById('deploy-target');
const recordBtn = document.getElementById('record-btn');
const stopBtn = document.getElementById('stop-btn');
const timerLabel = document.getElementById('timer');
const tabBar = document.getElementById('tab-bar');
const unifiedContainer = document.getElementById('unified-container');
const viewDeviceContainer = document.getElementById('view-device-container');
const experimentBar = document.querySelector('.experiment-bar');

const configSaveDir = document.getElementById('config-save-dir');
const configPrefix = document.getElementById('config-prefix');
const updateConfigBtn = document.getElementById('update-config-btn');

let socket;
const devices = {}; // { ip: { hostname, history, isPaused, tabOpen, charts } }
let activeView = 'dashboard';
let recordingStartTime = null;
let timerInterval = null;
let globalIsRecording = false;
let recordingIntervals = []; // [{ start: timeStr, end: timeStr }]

// Webcam state
let webcamStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let currentSessionTimestamp = null;
let currentSessionPrefix = "";

// Tag tracking state
let trackingTimer = null;
let trackingFrameCanvas = null;
let trackingInflight = false;
let trackingFpsSamples = [];
let lastTrackingTs = 0;
let zeroedIds = new Set();
let focusMarkerId = ""; // "" = auto (smallest ID with delta if any, else smallest seen)

const VOC_SMOOTHING = 0.01; 
const DATA_BUFFER_LIMIT = 3000; 
const LIVE_WINDOW_SIZE = 200;   

// Global Recording Plugin for Chart.js
const recordingPlugin = {
    id: 'recordingHighlight',
    beforeDraw: (chart) => {
        const ctx = chart.ctx;
        const xAxis = chart.scales.x;
        const yAxis = chart.scales.y;
        const labels = chart.data.labels;
        if (!labels || labels.length === 0) return;

        function drawZone(startStr, endStr, isLive) {
            let startIndex = -1, endIndex = -1;
            for (let i = 0; i < labels.length; i++) {
                if (startIndex === -1 && labels[i] >= startStr) startIndex = i;
                if (endStr && labels[i] >= endStr) { endIndex = i; break; }
            }
            if (isLive) endIndex = labels.length - 1;
            else if (endIndex === -1 && endStr) endIndex = labels.length - 1;

            if (startIndex !== -1 && endIndex !== -1 && startIndex <= endIndex) {
                const xStart = xAxis.getPixelForValue(startIndex);
                const xEnd = xAxis.getPixelForValue(endIndex);
                ctx.save();
                ctx.fillStyle = isLive ? 'rgba(46, 204, 113, 0.08)' : 'rgba(46, 204, 113, 0.15)';
                ctx.fillRect(xStart, yAxis.top, xEnd - xStart, yAxis.bottom - yAxis.top);
                if (isLive) {
                    ctx.fillStyle = 'rgba(46, 204, 113, 0.5)';
                    ctx.font = 'bold 10px Plus Jakarta Sans';
                    ctx.fillText('LIVE RECORDING', xStart + 10, yAxis.top + 20);
                }
                ctx.restore();
            }
        }
        recordingIntervals.forEach(interval => drawZone(interval.start, interval.end, false));
        if (globalIsRecording && recordingStartTime) {
            const currentStartStr = new Date(recordingStartTime).toLocaleTimeString('en-GB', {hour12:false});
            drawZone(currentStartStr, null, true);
        }
    }
};

Chart.register(recordingPlugin);

// Chart.js Global Pro Theme
Chart.defaults.color = 'rgba(255, 255, 255, 0.4)';
Chart.defaults.borderColor = 'rgba(255, 255, 255, 0.05)';
Chart.defaults.font.family = "'Plus Jakarta Sans', sans-serif";

// Recording control over the WebSocket needs the dashboard token whenever
// SENSOR_DASHBOARD_TOKEN is set on the PC running the dashboard. There is no
// token field in the UI: open the dashboard once as
// http://<host>:8000/?token=<secret> and the value is remembered in
// localStorage under the key "sensorDashboardToken" (you can also set that key
// directly from the browser console). With no token configured on the PC
// nothing here is needed — recording control then stays limited to localhost.
function getDashboardToken() {
    try {
        const fromUrl = new URLSearchParams(location.search).get('token');
        if (fromUrl) {
            localStorage.setItem('sensorDashboardToken', fromUrl);
            return fromUrl;
        }
        return localStorage.getItem('sensorDashboardToken');
    } catch (e) {
        return null;   // storage blocked (private mode etc.)
    }
}

// Headers for the dashboard's own POSTs to /api/*. The server requires the
// token there too whenever SENSOR_DASHBOARD_TOKEN is set.
function authHeaders() {
    const token = getDashboardToken();
    return token ? { 'X-Auth-Token': token } : {};
}

// Everything that reaches the page from the network (mDNS device names in
// particular, which any host on the LAN can announce) is inserted as text,
// never as markup: escapeHtml() for template strings, textContent elsewhere.
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Device IPs are interpolated into onclick handlers below, so only accept
// strings that can be an IPv4/IPv6 address.
function isSafeIp(ip) { return typeof ip === 'string' && /^[0-9A-Fa-f:.]{1,45}$/.test(ip); }

// Sensor values may be null (failed read on the Pi). Charts get null (a gap),
// readouts get "--"; nothing is shown as 0 unless the sensor reported 0.
function num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }
function fmt(v, digits) { const n = num(v); return n === null ? '--' : n.toFixed(digits); }
function vec3(v) { return Array.isArray(v) && v.length === 3 ? v.map(num) : [null, null, null]; }

function connect() {
    const token = getDashboardToken();
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const query = token ? `?token=${encodeURIComponent(token)}` : '';
    socket = new WebSocket(`${scheme}://${location.host}/ws${query}`);
    socket.onopen = () => {
        statusBadge.innerHTML = `<span style="color:var(--accent-green)">●</span> ONLINE`;
        document.getElementById('log-container').classList.add('show');
        addLog("Network connection established.");
    };
    socket.onclose = () => {
        statusBadge.innerHTML = `<span style="color:var(--accent-red)">●</span> OFFLINE`;
        setTimeout(connect, 2000);
    };
    socket.onmessage = (event) => handleMessage(JSON.parse(event.data));
}

function handleMessage(msg) {
    if (msg.type === "discovery") updateDiscovery(msg.devices);
    else if (msg.type === "connect") { if (isSafeIp(msg.ip)) addDevice(msg.ip, String(msg.hostname ?? 'Unknown Pi')); }
    else if (msg.type === "disconnect") removeDevice(msg.ip);
    else if (msg.type === "sensor_data") updateSensorData(msg);
    else if (msg.type === "recording_status") setRecordingUI(msg.is_recording, msg.path, msg.session_timestamp, msg.prefix);
    else if (msg.type === "config_update") updateConfigUI(msg);
    else if (msg.type === "error") addLog(`Rejected "${msg.action}": ${msg.detail}`);
}

function updateConfigUI(msg) {
    configSaveDir.value = msg.save_dir;
    configPrefix.value = msg.prefix;
}

function updateDiscovery(discoveryMap) {
    deployTargetSelect.innerHTML = '<option value="">Select Target Node</option>';
    for (const [host, ip] of Object.entries(discoveryMap)) {
        const opt = document.createElement('option');
        opt.value = ip;
        opt.textContent = `${host} (${ip})`;
        deployTargetSelect.appendChild(opt);
    }
}

function createChart(id, labelConfigs, dualAxis = false, onClickCallback = null, isAdvanced = false, historyRef = null) {
    const el = document.getElementById(id);
    if (!el) return null;
    const ctx = el.getContext('2d');
    const datasets = labelConfigs.map((cfg, idx) => ({
        label: cfg.label, borderColor: cfg.color, backgroundColor: cfg.color + "11",
        fill: true, data: historyRef ? historyRef.datasets[idx] : [], yAxisID: cfg.yAxis || 'y',
        tension: 0.3, borderWidth: 1.5, pointRadius: 0, hoverRadius: 6, hoverBackgroundColor: cfg.color, pointHitRadius: 20
    }));

    const chartObj = { isFollowing: true, isAdvanced: isAdvanced };
    const chart = new Chart(ctx, {
        type: 'line',
        data: { labels: historyRef ? historyRef.labels : [], datasets: datasets },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false, onClick: onClickCallback,
            interaction: { mode: 'index', intersect: false },
            hover: { mode: 'index', intersect: false },
            scales: {
                x: { display: isAdvanced, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 9 } } },
                y: { type: 'linear', position: 'left', ticks: { padding: 10 } },
                y1: dualAxis ? { type: 'linear', position: 'right', grid: { drawOnChartArea: false }, ticks: { padding: 10 } } : undefined
            },
            plugins: { 
                legend: { display: isAdvanced, position: 'top', align: 'end', labels: { boxWidth: 8, usePointStyle: true, font: { size: 10, weight: '600' } } },
                tooltip: {
                    enabled: isAdvanced, backgroundColor: 'rgba(2, 7, 37, 0.95)', titleFont: { size: 12, weight: '800' },
                    bodyFont: { size: 13, weight: '700', family: 'JetBrains Mono' }, padding: 12, cornerRadius: 10, borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1, displayColors: true,
                    callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y, 2)}` }
                },
                zoom: isAdvanced ? {
                    pan: { enabled: true, mode: 'x', onPan: () => { chartObj.isFollowing = false; } },
                    zoom: { wheel: { enabled: true, speed: 0.1 }, pinch: { enabled: true }, mode: 'x', onZoomStart: () => { chartObj.isFollowing = false; } }
                } : { zoom: { wheel: { enabled: false } }, pan: { enabled: false } }
            }
        },
        plugins: [{
            id: 'HTSCrosshair',
            afterDraw: (chart) => {
                if (chart.tooltip?._active?.length) {
                    const x = chart.tooltip._active[0].element.x;
                    const yAxis = chart.scales.y;
                    const ctx = chart.ctx;
                    ctx.save(); ctx.beginPath(); ctx.moveTo(x, yAxis.top); ctx.lineTo(x, yAxis.bottom);
                    ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)'; ctx.setLineDash([5, 5]); ctx.stroke(); ctx.restore();
                }
            }
        }]
    });
    chartObj.chart = chart;
    return chartObj;
}

function switchTab(viewId) {
    activeView = viewId;
    document.querySelectorAll('.view-content').forEach(v => { v.classList.add('hidden'); v.classList.remove('active'); });
    if (viewId === 'dashboard' || viewId === 'unified' || viewId === 'camera') {
        const el = document.getElementById(`view-${viewId}`);
        if (el) { el.classList.remove('hidden'); el.classList.add('active'); }
    } else {
        viewDeviceContainer.classList.remove('hidden');
        viewDeviceContainer.classList.add('active');
        document.querySelectorAll('.device-view').forEach(dv => dv.classList.add('hidden'));
        const el = document.getElementById(`view-${viewId}`);
        if (el) el.classList.remove('hidden');
    }
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    const tabEl = document.getElementById(`tab-${viewId}`);
    if (tabEl) tabEl.classList.add('active');

    if (viewId === 'camera') enterCameraView();
    else exitCameraView();
}

function getHistoryRef(ip, type) {
    const h = devices[ip].history;
    if (type === 'env') return { labels: h.labels, datasets: [h.temp, h.humi, h.voc] };
    if (type === 'light') return { labels: h.labels, datasets: [h.luxRaw, h.uv] };
    if (type === 'acc') return { labels: h.labels, datasets: [h.accX, h.accY, h.accZ] };
    if (type === 'gyro') return { labels: h.labels, datasets: [h.gyroX, h.gyroY, h.gyroZ] };
    return null;
}

function openDeviceTab(ip) {
    if (!devices[ip]) return;
    const ipSafe = ip.replace(/\./g, '-'), viewId = `device-${ipSafe}`;
    if (!devices[ip].tabOpen) {
        const tabBtn = document.createElement('button');
        tabBtn.id = `tab-${viewId}`;
        tabBtn.className = "tab-btn";
        tabBtn.onclick = () => switchTab(viewId);
        tabBtn.innerHTML = `${escapeHtml(devices[ip].hostname.replace('.local','').toUpperCase())} <span class="close-tab-btn" onclick="closeDeviceTab('${ip}', event)">✕</span>`;
        tabBar.appendChild(tabBtn);

        const deviceView = document.createElement('div');
        deviceView.id = `view-${viewId}`;
        deviceView.className = "device-view hidden space-y-10";
        deviceView.innerHTML = `
            <div class="flex items-center justify-between">
                <div><h2 class="text-3xl font-black tracking-tighter">${escapeHtml(devices[ip].hostname)}</h2><p style="color:var(--text-dim); font-size:11px; font-weight:600; letter-spacing:1px">${escapeHtml(ip)} • NODE ANALYSIS</p></div>
                <div style="display:flex; gap:12px"><button onclick="resetZoom('${ip}', null, 'indiv')" class="btn-ghost">RESET & FOLLOW</button><button onclick="togglePause('${ip}')" class="pause-btn-${ipSafe} btn-primary">PAUSE</button></div>
            </div>
            <div style="display:grid; grid-template-columns: repeat(2, 1fr); gap:30px">
                <div class="glass-card" style="padding:24px"><div class="chart-header-group"><h4 class="section-title" style="margin:0">Atmosphere</h4><div class="current-value-pill" id="pill-device-${ipSafe}-env">--</div></div><div class="chart-box cursor-zoom-in" onclick="openSensorDetailTab('${ip}', 'env')"><canvas id="chart-indiv-${ipSafe}-env"></canvas></div></div>
                <div class="glass-card" style="padding:24px"><div class="chart-header-group"><h4 class="section-title" style="margin:0">Luminance</h4><div class="current-value-pill" id="pill-device-${ipSafe}-light">--</div></div><div class="chart-box cursor-zoom-in" onclick="openSensorDetailTab('${ip}', 'light')"><canvas id="chart-indiv-${ipSafe}-light"></canvas></div></div>
                <div class="glass-card" style="padding:24px"><div class="chart-header-group"><h4 class="section-title" style="margin:0">Accelerometer</h4><div class="current-value-pill" id="pill-device-${ipSafe}-acc">--</div></div><div class="chart-box cursor-zoom-in" onclick="openSensorDetailTab('${ip}', 'acc')"><canvas id="chart-indiv-${ipSafe}-acc"></canvas></div></div>
                <div class="glass-card" style="padding:24px"><div class="chart-header-group"><h4 class="section-title" style="margin:0">Gyroscope</h4><div class="current-value-pill" id="pill-device-${ipSafe}-gyro">--</div></div><div class="chart-box cursor-zoom-in" onclick="openSensorDetailTab('${ip}', 'gyro')"><canvas id="chart-indiv-${ipSafe}-gyro"></canvas></div></div>
            </div>`;
        viewDeviceContainer.appendChild(deviceView);
        devices[ip].charts.indiv = {
            env: createChart(`chart-indiv-${ipSafe}-env`, [{label:'Temp', color:'#ff5555'}, {label:'Humi', color:'#5555ff'}, {label:'VOC', color:'#ffff55', yAxis:'y1'}], true, null, true, getHistoryRef(ip, 'env')),
            light: createChart(`chart-indiv-${ipSafe}-light`, [{label:'Light ch0 (raw)', color:'#ffaa00'}, {label:'UV', color:'#aa00ff'}], false, null, true, getHistoryRef(ip, 'light')),
            acc: createChart(`chart-indiv-${ipSafe}-acc`, [{label:'X', color:'#ff5555'}, {label:'Y', color:'#55ff55'}, {label:'Z', color:'#5555ff'}], false, null, true, getHistoryRef(ip, 'acc')),
            gyro: createChart(`chart-indiv-${ipSafe}-gyro`, [{label:'X', color:'#ff5555'}, {label:'Y', color:'#55ff55'}, {label:'Z', color:'#5555ff'}], false, null, true, getHistoryRef(ip, 'gyro'))
        };
        devices[ip].tabOpen = true;
    }
    switchTab(viewId);
}

function openSensorDetailTab(ip, type) {
    const ipSafe = ip.replace(/\./g, '-'), viewId = `detail-${ipSafe}-${type}`;
    if (!document.getElementById(`tab-${viewId}`)) {
        const tabBtn = document.createElement('button');
        tabBtn.id = `tab-${viewId}`; tabBtn.className = "tab-btn"; tabBtn.onclick = () => switchTab(viewId);
        tabBtn.innerHTML = `${type.toUpperCase()} ANALYSIS <span class="close-tab-btn" onclick="closeSensorDetailTab('${viewId}', '${ip}', '${type}', event)">✕</span>`;
        tabBar.appendChild(tabBtn);
        const detailView = document.createElement('div');
        detailView.id = `view-${viewId}`; detailView.className = "device-view hidden space-y-8";
        detailView.innerHTML = `
            <div class="flex items-center justify-between">
                <div><h2 class="text-4xl font-black tracking-tighter">${type.toUpperCase()}</h2><p style="color:var(--text-dim); font-size:12px; font-weight:600; letter-spacing:1px; margin-top:4px">${escapeHtml(devices[ip].hostname)}</p></div>
                <div style="display:flex; gap:12px"><button onclick="resetZoom('${ip}', '${type}', 'detail')" class="btn-ghost">RESET & FOLLOW</button><button onclick="togglePause('${ip}')" class="pause-btn-${ipSafe} btn-primary">PAUSE</button></div>
            </div>
            <div class="glass-card" style="height:72vh; position:relative; overflow:hidden; display:flex; flex-direction:column;">
                <div class="chart-header-group" style="padding: 0 10px 20px 10px;"><span class="section-title" style="margin:0">Live Analysis</span><div id="val-detail-${ipSafe}-${type}" style="font-size:24px; font-weight:800; font-family:'JetBrains Mono'; color:var(--accent-yellow)">--</div></div>
                <div style="flex:1; position:relative;"><canvas id="chart-detail-${ipSafe}-${type}"></canvas></div>
            </div>`;
        viewDeviceContainer.appendChild(detailView);
        let configs, dual = false;
        if (type === 'env') { configs = [{label:'Temp', color:'#ff5555'}, {label:'Humi', color:'#5555ff'}, {label:'VOC', color:'#ffff55', yAxis:'y1'}]; dual = true; }
        else if (type === 'light') configs = [{label:'Light ch0 (raw)', color:'#ffaa00'}, {label:'UV', color:'#aa00ff'}];
        else configs = [{label:'X', color:'#ff5555'}, {label:'Y', color:'#55ff55'}, {label:'Z', color:'#5555ff'}];
        if (!devices[ip].charts.detail) devices[ip].charts.detail = {};
        devices[ip].charts.detail[type] = createChart(`chart-detail-${ipSafe}-${type}`, configs, dual, null, true, getHistoryRef(ip, type));
    }
    switchTab(viewId);
}

function resetZoom(ip, type, group) {
    if (group === 'detail') { const obj = devices[ip]?.charts?.detail?.[type]; if (obj) { obj.chart.resetZoom(); obj.isFollowing = true; } }
    else if (group === 'indiv') Object.values(devices[ip]?.charts?.indiv || {}).forEach(obj => { obj.chart.resetZoom(); obj.isFollowing = true; });
}

function closeSensorDetailTab(viewId, ip, type, event) { if (event) event.stopPropagation(); document.getElementById(`tab-${viewId}`)?.remove(); document.getElementById(`view-${viewId}`)?.remove(); if (devices[ip]?.charts?.detail?.[type]) { devices[ip].charts.detail[type].chart.destroy(); delete devices[ip].charts.detail[type]; } if (activeView === viewId) switchTab('unified'); }
function closeDeviceTab(ip, event) { if (event) event.stopPropagation(); if (!devices[ip]) return; const ipSafe = ip.replace(/\./g, '-'), viewId = `device-${ipSafe}`; document.getElementById(`tab-${viewId}`)?.remove(); document.getElementById(`view-${viewId}`)?.remove(); devices[ip].tabOpen = false; if (devices[ip].charts.indiv) { Object.values(devices[ip].charts.indiv).forEach(c => c.chart.destroy()); delete devices[ip].charts.indiv; } if (activeView === viewId) switchTab('dashboard'); }

function togglePause(ip) {
    if (devices[ip]) {
        devices[ip].isPaused = !devices[ip].isPaused;
        const ipSafe = ip.replace(/\./g, '-');
        const btns = document.querySelectorAll(`.pause-btn-${ipSafe}`);
        btns.forEach(btn => { btn.innerHTML = devices[ip].isPaused ? "RESUME" : "PAUSE"; btn.style.background = devices[ip].isPaused ? "var(--accent-yellow)" : "var(--text-main)"; });
    }
}

function openTargetFolder(path = null) { socket.send(JSON.stringify({ action: "open_folder", path })); }
function pickTargetFolder() { socket.send(JSON.stringify({ action: "pick_folder" })); }

function addDevice(ip, hostname) {
    if (devices[ip]) return;
    const ipSafe = ip.replace(/\./g, '-');
    const item = document.createElement('div');
    item.id = `card-${ipSafe}`; item.className = "device-status-item";
    item.innerHTML = `<div style="display:flex; align-items:center; gap:20px"><div style="width:10px; height:10px; border-radius:50%; background:var(--accent-green); box-shadow: 0 0 10px var(--accent-green)"></div><div><h4 style="font-weight:800; font-size:16px">${escapeHtml(hostname)}</h4><p style="font-size:11px; color:var(--text-dim); font-weight:600; font-family:'JetBrains Mono'">${escapeHtml(ip)}</p></div></div><button onclick="switchTab('unified')" class="btn-primary">MONITOR LIVE</button>`;
    deviceContainer.appendChild(item);

    devices[ip] = { hostname, isPaused: false, smoothVoc: null, tabOpen: false, history: { labels: [], temp: [], humi: [], voc: [], luxRaw: [], uv: [], accX: [], accY: [], accZ: [], gyroX: [], gyroY: [], gyroZ: [] }, charts: { unified: null, indiv: null, detail: {} } };

    const unifiedSection = document.createElement('div');
    unifiedSection.id = `unified-sec-${ipSafe}`; unifiedSection.className = "glass-card";
    unifiedSection.innerHTML = `
        <div class="flex items-center justify-between" style="margin-bottom:30px; border-bottom:1px solid var(--border-glass); padding-bottom:20px">
            <div style="display:flex; align-items:center; gap:15px"><div style="width:40px; height:40px; background:rgba(255,255,255,0.05); border-radius:12px; display:flex; align-items:center; justify-content:center; font-size:20px">📱</div><div><h3 class="text-xl font-bold">${escapeHtml(hostname)}</h3><p style="font-size:10px; color:var(--text-dim); font-family:'JetBrains Mono'">${escapeHtml(ip)}</p></div></div>
            <div style="display:flex; gap:10px"><button onclick="togglePause('${ip}')" class="pause-btn-${ipSafe} btn-ghost">PAUSE</button><button onclick="openDeviceTab('${ip}')" class="btn-primary">FULL ANALYSIS</button></div>
        </div>
        <div style="display:grid; grid-template-columns: repeat(2, 1fr); gap:24px">
            <div><div class="chart-header-group"><h4 class="section-title" style="margin:0">Environment</h4><div class="current-value-pill" id="val-unified-${ipSafe}-env">--</div></div><div class="chart-box cursor-pointer" onclick="openSensorDetailTab('${ip}', 'env')"><canvas id="chart-unified-${ipSafe}-env"></canvas></div></div>
            <div><div class="chart-header-group"><h4 class="section-title" style="margin:0">Motion</h4><div class="current-value-pill" id="val-unified-${ipSafe}-acc">--</div></div><div class="chart-box cursor-pointer" onclick="openSensorDetailTab('${ip}', 'acc')"><canvas id="chart-unified-${ipSafe}-acc"></canvas></div></div>
        </div>`;
    unifiedContainer.appendChild(unifiedSection);

    devices[ip].charts.unified = {
        env: createChart(`chart-unified-${ipSafe}-env`, [{label:'Temp', color:'#ff5555'}, {label:'VOC', color:'#ffff55', yAxis:'y1'}], true, null, false, getHistoryRef(ip, 'env')),
        acc: createChart(`chart-unified-${ipSafe}-acc`, [{label:'X', color:'#ff5555'}, {label:'Y', color:'#55ff55'}, {label:'Z', color:'#5555ff'}], false, null, false, getHistoryRef(ip, 'acc'))
    };
    updateActiveCount();
}

// Called on the server's "disconnect" message (it was referenced but never
// defined before, so every Pi disconnect threw in the message handler).
function removeDevice(ip) {
    if (!devices[ip]) return;
    const ipSafe = ip.replace(/\./g, '-');
    if (devices[ip].tabOpen) closeDeviceTab(ip);
    Object.keys(devices[ip].charts.detail || {}).forEach(type => closeSensorDetailTab(`detail-${ipSafe}-${type}`, ip, type));
    Object.values(devices[ip].charts.unified || {}).forEach(c => c && c.chart.destroy());
    document.getElementById(`card-${ipSafe}`)?.remove();
    document.getElementById(`unified-sec-${ipSafe}`)?.remove();
    delete devices[ip];
    updateActiveCount();
    addLog(`Node ${ip} disconnected.`);
}

function updateSensorData(msg) {
    const ip = msg.ip; if (!devices[ip]) return;
    const dev = devices[ip], ipSafe = ip.replace(/\./g, '-'), rawVoc = num(msg.voc);
    if (rawVoc !== null) {
        if (dev.smoothVoc === null) dev.smoothVoc = rawVoc;
        else dev.smoothVoc = (rawVoc * VOC_SMOOTHING) + (dev.smoothVoc * (1 - VOC_SMOOTHING));
    }
    const temp = num(msg.temp), humi = num(msg.humi), luxRaw = num(msg.lux_raw ?? msg.lux), uv = num(msg.uv);
    const vocShown = rawVoc === null ? null : Math.round(dev.smoothVoc);
    const displayVoc = vocShown === null ? '--' : vocShown;
    const acc = vec3(msg.accel), gyr = vec3(msg.gyro), now = new Date().toLocaleTimeString('en-GB', {hour12:false});
    const h = dev.history; h.labels.push(now); h.temp.push(temp); h.humi.push(humi); h.voc.push(vocShown); h.luxRaw.push(luxRaw); h.uv.push(uv); h.accX.push(acc[0]); h.accY.push(acc[1]); h.accZ.push(acc[2]); h.gyroX.push(gyr[0]); h.gyroY.push(gyr[1]); h.gyroZ.push(gyr[2]);
    let didShift = false; if (h.labels.length > DATA_BUFFER_LIMIT) { h.labels.shift(); h.temp.shift(); h.humi.shift(); h.voc.shift(); h.luxRaw.shift(); h.uv.shift(); h.accX.shift(); h.accY.shift(); h.accZ.shift(); h.gyroX.shift(); h.gyroY.shift(); h.gyroZ.shift(); didShift = true; }
    const set = (id, val) => { const e = document.getElementById(id); if(e) e.textContent = val; };
    const accText = `X:${fmt(acc[0], 2)} Y:${fmt(acc[1], 2)} Z:${fmt(acc[2], 2)}`;
    const gyrText = `X:${fmt(gyr[0], 1)} Y:${fmt(gyr[1], 1)} Z:${fmt(gyr[2], 1)}`;
    set(`val-unified-${ipSafe}-env`, `${fmt(temp, 1)}° / ${displayVoc} VOC`);
    set(`val-unified-${ipSafe}-acc`, `Z: ${fmt(acc[2], 2)}`);
    if (dev.tabOpen) {
        set(`pill-device-${ipSafe}-env`, `${fmt(temp, 1)}° / ${fmt(humi, 0)}% / ${displayVoc} VOC`);
        set(`pill-device-${ipSafe}-light`, `Ch0 raw: ${luxRaw ?? '--'} / UV: ${uv ?? '--'}`);
        set(`pill-device-${ipSafe}-acc`, accText);
        set(`pill-device-${ipSafe}-gyro`, gyrText);
    }
    if (dev.charts.detail) {
        Object.keys(dev.charts.detail).forEach(type => {
            const el = document.getElementById(`val-detail-${ipSafe}-${type}`);
            if (el) {
                if (type === 'env') el.textContent = `${fmt(temp, 1)}° / ${displayVoc} VOC / ${fmt(humi, 0)}% HUM`;
                else if (type === 'light') el.textContent = `${luxRaw ?? '--'} CH0 RAW / ${uv ?? '--'} UV`;
                else if (type === 'acc') el.textContent = accText;
                else if (type === 'gyro') el.textContent = gyrText;
            }
        });
    }
    const allGroups = [dev.charts.unified, dev.charts.indiv, dev.charts.detail];
    allGroups.forEach(group => { if (!group) return; Object.values(group).forEach(obj => { if (obj && !obj.isFollowing && didShift) { obj.chart.options.scales.x.min -= 1; obj.chart.options.scales.x.max -= 1; } }); });
}

function startRenderingLoop() {
    setInterval(() => {
        for (const ip in devices) {
            const dev = devices[ip]; if (dev.isPaused) continue;
            const sync = (chartObj, isVisible) => {
                if (!chartObj || !isVisible) return; const c = chartObj.chart;
                if (chartObj.isFollowing) { const len = c.data.labels.length; c.options.scales.x.min = len > LIVE_WINDOW_SIZE ? len - LIVE_WINDOW_SIZE : 0; c.options.scales.x.max = len - 1; }
                c.update('none');
            };
            if (activeView === 'unified' && dev.charts.unified) Object.values(dev.charts.unified).forEach(c => sync(c, true));
            const ipSafe = ip.replace(/\./g, '-');
            if (activeView === `device-${ipSafe}` && dev.charts.indiv) Object.values(dev.charts.indiv).forEach(c => sync(c, true));
            if (dev.charts.detail) { for (const type in dev.charts.detail) { if (activeView === `detail-${ipSafe}-${type}`) sync(dev.charts.detail[type], true); } }
        }
    }, 200); 
}

function updateActiveCount() { activeCountLabel.textContent = `${Object.keys(devices).length} NODES ONLINE`; }
function addLog(text) { const t = new Date().toLocaleTimeString('en-GB', {hour12:false}), e = document.createElement('div'); e.textContent = `[${t}] ${text}`; logs.appendChild(e); logs.scrollTop = logs.scrollHeight; }

function setRecordingUI(is, savedPath = null, sessionTimestamp = null, prefix = null) {
    globalIsRecording = is;
    if (is) {
        recordingStartTime = Date.now();
        recordingIntervals.push({ start: new Date().toLocaleTimeString('en-GB', {hour12:false}), end: null });
        if (sessionTimestamp) currentSessionTimestamp = sessionTimestamp;
        if (prefix !== null && prefix !== undefined) currentSessionPrefix = prefix;
        startWebcamRecording();
    }
    else if (recordingIntervals.length > 0) { recordingIntervals[recordingIntervals.length - 1].end = new Date().toLocaleTimeString('en-GB', {hour12:false}); }
    recordBtn.innerHTML = is ? "RECORDING..." : "START RECORDING"; recordBtn.style.background = is ? "var(--accent-red)" : "var(--accent-green)";
    stopBtn.disabled = !is; experimentBar.className = is ? "experiment-bar active" : "experiment-bar";
    if (is) { timerInterval = setInterval(() => { const el = (Date.now()-recordingStartTime)/1000; timerLabel.textContent = `${Math.floor(el/60).toString().padStart(2,'0')}:${(el%60).toFixed(1).padStart(4,'0')}`; }, 100); addLog("Recording started."); }
    else { clearInterval(timerInterval); timerLabel.textContent = "00:00.0"; addLog("Recording saved."); stopWebcamRecording(); if(savedPath) openTargetFolder(savedPath); }
    ensureTrackingState();
}

// --- Webcam Recording ---
async function enableWebcam() {
    const videoConstraints = { width: { ideal: 1280 }, height: { ideal: 720 } };
    let withAudio = true;
    try {
        webcamStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: true });
    } catch (e1) {
        try {
            webcamStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: false });
            withAudio = false;
        } catch (e2) {
            addLog(`Webcam error: ${e2.message}`);
            return false;
        }
    }
    // Default: mic muted on enable (user can unmute via MIC button)
    if (withAudio) {
        webcamStream.getAudioTracks().forEach(t => { t.enabled = false; });
    }
    document.getElementById('webcam-video').srcObject = webcamStream;
    const trackVideo = document.getElementById('tracking-video');
    if (trackVideo) trackVideo.srcObject = webcamStream;
    document.getElementById('webcam-preview').classList.remove('hidden');
    document.getElementById('webcam-toggle-label').textContent = withAudio ? 'WEBCAM ON' : 'WEBCAM (NO MIC)';
    updateMicButton();
    addLog(withAudio ? "Webcam enabled (mic muted by default)." : "Webcam enabled (video only — mic denied).");
    if (activeView === 'camera') enterCameraView();
    ensureTrackingState();
    return true;
}

function updateMicButton() {
    const btn = document.getElementById('webcam-mic-btn');
    if (!webcamStream) return;
    const audioTracks = webcamStream.getAudioTracks();
    if (audioTracks.length === 0) {
        btn.textContent = 'NO MIC';
        btn.className = 'webcam-mic unavailable';
        btn.disabled = true;
        return;
    }
    btn.disabled = false;
    const enabled = audioTracks[0].enabled;
    btn.textContent = enabled ? 'MIC ON' : 'MIC OFF';
    btn.classList.toggle('muted', !enabled);
}

function toggleMic() {
    if (!webcamStream) return;
    const audioTracks = webcamStream.getAudioTracks();
    if (audioTracks.length === 0) return;
    const newState = !audioTracks[0].enabled;
    audioTracks.forEach(t => { t.enabled = newState; });
    updateMicButton();
    addLog(newState ? "Mic unmuted." : "Mic muted.");
}

document.getElementById('webcam-mic-btn').addEventListener('click', toggleMic);

// Minimize / restore preview
document.getElementById('webcam-min-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    const preview = document.getElementById('webcam-preview');
    const btn = e.currentTarget;
    const minimized = preview.classList.toggle('minimized');
    btn.textContent = minimized ? '▢' : '−';
    btn.title = minimized ? 'Restore' : 'Minimize';
});

function disableWebcam() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        try { mediaRecorder.stop(); } catch {}
    }
    if (webcamStream) {
        webcamStream.getTracks().forEach(t => t.stop());
        webcamStream = null;
    }
    document.getElementById('webcam-video').srcObject = null;
    const trackVideo = document.getElementById('tracking-video');
    if (trackVideo) trackVideo.srcObject = null;
    stopTrackingLoop();
    const banner = document.getElementById('tracking-banner');
    if (banner) {
        banner.classList.remove('hidden');
        banner.textContent = "Enable WEBCAM in the bottom bar to start tracking.";
    }
    document.getElementById('webcam-preview').classList.add('hidden');
    document.getElementById('webcam-toggle-label').textContent = 'WEBCAM OFF';
    addLog("Webcam disabled.");
}

function startWebcamRecording() {
    if (!webcamStream) return;
    recordedChunks = [];
    const candidates = [
        'video/webm;codecs=vp9,opus',
        'video/webm;codecs=vp8,opus',
        'video/webm',
    ];
    let opts = {};
    for (const m of candidates) {
        if (MediaRecorder.isTypeSupported(m)) { opts.mimeType = m; break; }
    }
    try {
        mediaRecorder = new MediaRecorder(webcamStream, opts);
    } catch (e) {
        addLog(`MediaRecorder failed: ${e.message}`);
        return;
    }
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = uploadWebcamRecording;
    mediaRecorder.start(1000);
    const s = document.getElementById('webcam-status');
    s.textContent = 'REC ●';
    s.classList.add('recording');
}

function stopWebcamRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
    }
    const s = document.getElementById('webcam-status');
    s.textContent = 'STANDBY';
    s.classList.remove('recording');
}

async function uploadWebcamRecording() {
    if (recordedChunks.length === 0) return;
    const blob = new Blob(recordedChunks, { type: 'video/webm' });
    recordedChunks = [];
    const ts = currentSessionTimestamp || new Date().toISOString().replace(/[-:T.Z]/g, '').slice(0, 14);
    const fd = new FormData();
    fd.append('video', blob, `webcam_${ts}.webm`);
    fd.append('timestamp', ts);
    fd.append('prefix', currentSessionPrefix || '');
    addLog(`Uploading webcam (${(blob.size/1024/1024).toFixed(1)} MB)...`);
    try {
        const r = await fetch('/api/upload_video', { method: 'POST', body: fd, headers: authHeaders() });
        const j = await r.json();
        const fname = (j.path || '').split(/[\\/]/).pop();
        addLog(`Webcam saved: ${fname}`);
    } catch (e) {
        addLog(`Webcam upload failed: ${e.message}`);
    }
}

document.getElementById('webcam-toggle').addEventListener('change', async (e) => {
    if (e.target.checked) {
        const ok = await enableWebcam();
        if (!ok) e.target.checked = false;
    } else {
        disableWebcam();
    }
});

// --- AprilTag Tracking ---
function shouldRunTracking() {
    if (!webcamStream) return false;
    if (activeView === 'camera') return true;
    if (globalIsRecording) return true;
    return false;
}

function ensureTrackingState() {
    const should = shouldRunTracking();
    const running = !!trackingTimer;
    if (should && !running) startTrackingLoop();
    else if (!should && running) stopTrackingLoop();
}

function enterCameraView() {
    const video = document.getElementById('tracking-video');
    const banner = document.getElementById('tracking-banner');
    if (!webcamStream) {
        banner.classList.remove('hidden');
        banner.textContent = "Enable WEBCAM in the bottom bar to start tracking.";
        video.srcObject = null;
        return;
    }
    banner.classList.add('hidden');
    if (video.srcObject !== webcamStream) video.srcObject = webcamStream;
    ensureTrackingState();
}

function exitCameraView() {
    // Keep tracking-video.srcObject attached so background detection (during recording) keeps a frame source.
    ensureTrackingState();
}

function pickCaptureVideo() {
    const big = document.getElementById('tracking-video');
    if (big && big.readyState >= 2 && big.videoWidth > 0) return big;
    const small = document.getElementById('webcam-video');
    if (small && small.readyState >= 2 && small.videoWidth > 0) return small;
    return null;
}

function startTrackingLoop() {
    stopTrackingLoop();
    if (!webcamStream) return;
    trackingFrameCanvas = trackingFrameCanvas || document.createElement('canvas');
    const tick = async () => {
        const enabled = document.getElementById('tracking-enabled')?.checked;
        if (!enabled || trackingInflight) return;
        const video = pickCaptureVideo();
        if (!video) return;
        const w = video.videoWidth, h = video.videoHeight;
        if (!w || !h) return;

        trackingFrameCanvas.width = w;
        trackingFrameCanvas.height = h;
        trackingFrameCanvas.getContext('2d').drawImage(video, 0, 0, w, h);

        const blob = await new Promise(r => trackingFrameCanvas.toBlob(r, 'image/jpeg', 0.7));
        if (!blob) return;

        const tagSizeMm = parseFloat(document.getElementById('tag-size-input').value) || 25;
        const smoothOn = document.getElementById('tracking-smooth')?.checked ? 1 : 0;
        const fd = new FormData();
        fd.append('image', blob, 'frame.jpg');
        fd.append('tag_size_mm', String(tagSizeMm));
        fd.append('frame_w', String(w));
        fd.append('frame_h', String(h));
        fd.append('smooth', String(smoothOn));

        trackingInflight = true;
        const tStart = performance.now();
        try {
            const r = await fetch('/api/detect_tags', { method: 'POST', body: fd, headers: authHeaders() });
            const j = await r.json();
            renderTrackingResults(j.markers || [], w, h);
            updateTrackingFps(performance.now() - tStart);
        } catch (e) {
            // ignore transient errors
        } finally {
            trackingInflight = false;
        }
    };
    const rate = parseFloat(document.getElementById('tracking-rate-input').value) || 10;
    const intervalMs = Math.max(33, 1000 / rate);
    trackingTimer = setInterval(tick, intervalMs);
}

function stopTrackingLoop() {
    if (trackingTimer) { clearInterval(trackingTimer); trackingTimer = null; }
    trackingFpsSamples = [];
    const fpsEl = document.getElementById('tracking-fps');
    if (fpsEl) fpsEl.textContent = '-- Hz';
    const ctx = document.getElementById('tracking-overlay')?.getContext('2d');
    if (ctx) ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    const tbody = document.getElementById('markers-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="markers-empty">— no markers detected —</td></tr>';
}

function updateTrackingFps(latencyMs) {
    const now = performance.now();
    if (lastTrackingTs > 0) {
        const dt = now - lastTrackingTs;
        if (dt > 0) {
            trackingFpsSamples.push(1000 / dt);
            if (trackingFpsSamples.length > 10) trackingFpsSamples.shift();
        }
    }
    lastTrackingTs = now;
    if (trackingFpsSamples.length > 0) {
        const avg = trackingFpsSamples.reduce((a, b) => a + b, 0) / trackingFpsSamples.length;
        const el = document.getElementById('tracking-fps');
        if (el) el.textContent = `${avg.toFixed(1)} Hz / ${latencyMs.toFixed(0)}ms`;
    }
}

function renderTrackingResults(markers, frameW, frameH) {
    if (activeView !== 'camera') return;  // off-tab: detection still ran (server logs to CSV), but skip DOM updates
    const canvas = document.getElementById('tracking-overlay');
    if (!canvas) return;
    canvas.width = frameW;
    canvas.height = frameH;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, frameW, frameH);

    ctx.lineWidth = Math.max(2, frameW / 480);
    ctx.font = `bold ${Math.max(14, Math.round(frameW / 50))}px 'JetBrains Mono', monospace`;
    ctx.lineJoin = 'round';

    const cornerColors = ['#2ECC71', '#FF4D4D', '#4E6CCD', '#F9CF31'];

    markers.forEach((m) => {
        const pts = m.corners;
        ctx.strokeStyle = m.outlier ? '#F9CF31' : '#2ECC71';
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (let i = 1; i < 4; i++) ctx.lineTo(pts[i][0], pts[i][1]);
        ctx.closePath();
        ctx.stroke();

        // corner dots
        pts.forEach((p, i) => {
            ctx.fillStyle = cornerColors[i];
            ctx.beginPath();
            ctx.arc(p[0], p[1], Math.max(4, frameW / 240), 0, Math.PI * 2);
            ctx.fill();
        });

        // ID label
        const cx = (pts[0][0] + pts[1][0] + pts[2][0] + pts[3][0]) / 4;
        const cy = (pts[0][1] + pts[1][1] + pts[2][1] + pts[3][1]) / 4;
        const label = `ID ${m.id}`;
        const metrics = ctx.measureText(label);
        const padX = 8, padY = 6;
        ctx.fillStyle = 'rgba(0,0,0,0.75)';
        ctx.fillRect(cx - metrics.width/2 - padX, cy - 12 - padY, metrics.width + padX*2, 24 + padY);
        ctx.fillStyle = '#fff';
        ctx.fillText(label, cx - metrics.width/2, cy + 6);

        // Z-distance label below ID
        const zLabel = `${m.tz_mm.toFixed(0)}mm`;
        ctx.font = `bold ${Math.max(11, Math.round(frameW / 80))}px 'JetBrains Mono', monospace`;
        const zMetrics = ctx.measureText(zLabel);
        ctx.fillStyle = 'rgba(249,207,49,0.95)';
        ctx.fillText(zLabel, cx - zMetrics.width/2, cy + 28);
        // restore font for next marker label
        ctx.font = `bold ${Math.max(14, Math.round(frameW / 50))}px 'JetBrains Mono', monospace`;
    });

    // Update marker table
    const tbody = document.getElementById('markers-tbody');
    if (!tbody) return;
    if (markers.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="markers-empty">— no markers detected —</td></tr>';
    } else {
        markers.sort((a, b) => a.id - b.id);
        tbody.innerHTML = markers.map(m => {
            const d = m.delta;
            const tx = d ? d.dx_mm : m.tx_mm;
            const ty = d ? d.dy_mm : m.ty_mm;
            const tz = d ? d.dz_mm : m.tz_mm;
            const r  = d ? d.droll  : m.roll;
            const p  = d ? d.dpitch : m.pitch;
            const yw = d ? d.dyaw   : m.yaw;
            const idCell = d
                ? `<td class="id-cell">${m.id}<span style="color:var(--accent-yellow)">Δ</span></td>`
                : `<td class="id-cell">${m.id}</td>`;
            const fmt = (v, p) => d ? fmtDeltaSigned(v, p) : applyDeadzone(v).toFixed(p);
            return `<tr class="${d ? 'has-delta' : ''}">
                ${idCell}
                <td>${fmt(tx, 0)}</td>
                <td>${fmt(ty, 0)}</td>
                <td>${fmt(tz, 0)}</td>
                <td>${fmt(r, 1)}</td>
                <td>${fmt(p, 1)}</td>
                <td>${fmt(yw, 1)}</td>
            </tr>`;
        }).join('');
    }

    updateFocusReadout(markers);
    updateFocusOptions(markers);
}

function getDeadzone() {
    const v = parseFloat(document.getElementById('deadzone-input')?.value);
    return Number.isFinite(v) && v >= 0 ? v : 0;
}
function applyDeadzone(v) {
    return Math.abs(v) < getDeadzone() ? 0 : v;
}

function fmtDeltaSigned(v, decimals) {
    v = applyDeadzone(v);
    const sign = v > 0 ? '+' : '';
    const cls = v > 0 ? 'delta-pos' : (v < 0 ? 'delta-neg' : '');
    return `<span class="${cls}">${sign}${v.toFixed(decimals)}</span>`;
}

function updateFocusOptions(markers) {
    const sel = document.getElementById('focus-marker-select');
    if (!sel) return;
    const wantedIds = markers.map(m => String(m.id));
    const existingIds = Array.from(sel.options).slice(1).map(o => o.value);
    if (wantedIds.length !== existingIds.length || wantedIds.some((id, i) => id !== existingIds[i])) {
        const current = sel.value;
        sel.innerHTML = '<option value="">— auto —</option>' +
            wantedIds.map(id => `<option value="${id}">ID ${id}</option>`).join('');
        if (wantedIds.includes(current)) sel.value = current;
    }
}

function updateFocusReadout(markers) {
    const el = document.getElementById('big-readout');
    if (!el) return;
    if (markers.length === 0) {
        el.innerHTML = '<div class="big-readout-empty">No marker selected.</div>';
        return;
    }
    let target = null;
    if (focusMarkerId !== "") {
        target = markers.find(m => String(m.id) === focusMarkerId);
    }
    if (!target) {
        // auto: prefer one with delta, else smallest ID
        const withDelta = markers.filter(m => m.delta);
        target = withDelta.length ? withDelta[0] : markers[0];
    }
    const d = target.delta;
    const useDelta = !!d;
    const tx = useDelta ? d.dx_mm : target.tx_mm;
    const ty = useDelta ? d.dy_mm : target.ty_mm;
    const tz = useDelta ? d.dz_mm : target.tz_mm;
    const r  = useDelta ? d.droll  : target.roll;
    const p  = useDelta ? d.dpitch : target.pitch;
    const yw = useDelta ? d.dyaw   : target.yaw;

    const fmtNum = (v, dec) => {
        v = applyDeadzone(v);
        const sign = useDelta && v > 0 ? '+' : '';
        return sign + v.toFixed(dec);
    };
    const colorOf = (v) => {
        if (!useDelta) return 'white';
        v = applyDeadzone(v);
        if (v > 0) return 'var(--accent-green)';
        if (v < 0) return 'var(--accent-red)';
        return 'var(--text-dim)';
    };
    const lbl = useDelta ? ['ΔX', 'ΔY', 'ΔZ', 'ΔRoll', 'ΔPitch', 'ΔYaw']
                         : ['X', 'Y', 'Z', 'Roll', 'Pitch', 'Yaw'];
    el.innerHTML = `
        <div class="big-readout-id">ID ${target.id} ${useDelta ? '<span style="color:var(--accent-yellow)">[ Δ from zero ]</span>' : '<span style="color:var(--text-dim)">[ absolute ]</span>'}</div>
        <div class="big-readout-grid">
            <div class="big-readout-cell"><div class="axis-label">${lbl[0]}</div><div class="axis-value" style="color:${colorOf(tx)}">${fmtNum(tx, 0)}</div><div class="axis-unit">mm</div></div>
            <div class="big-readout-cell"><div class="axis-label">${lbl[1]}</div><div class="axis-value" style="color:${colorOf(ty)}">${fmtNum(ty, 0)}</div><div class="axis-unit">mm</div></div>
            <div class="big-readout-cell"><div class="axis-label">${lbl[2]}</div><div class="axis-value" style="color:${colorOf(tz)}">${fmtNum(tz, 0)}</div><div class="axis-unit">mm</div></div>
        </div>
        <div class="big-readout-row-divider"></div>
        <div class="big-readout-grid">
            <div class="big-readout-cell"><div class="axis-label">${lbl[3]}</div><div class="axis-value" style="color:${colorOf(r)}">${fmtNum(r, 1)}</div><div class="axis-unit">deg</div></div>
            <div class="big-readout-cell"><div class="axis-label">${lbl[4]}</div><div class="axis-value" style="color:${colorOf(p)}">${fmtNum(p, 1)}</div><div class="axis-unit">deg</div></div>
            <div class="big-readout-cell"><div class="axis-label">${lbl[5]}</div><div class="axis-value" style="color:${colorOf(yw)}">${fmtNum(yw, 1)}</div><div class="axis-unit">deg</div></div>
        </div>`;
}

function setHeaderMode(isRelative) {
    const tag = document.getElementById('markers-mode-tag');
    if (tag) {
        tag.textContent = isRelative ? 'RELATIVE Δ' : 'ABSOLUTE';
        tag.classList.toggle('relative', isRelative);
    }
    const labels = isRelative
        ? ['ΔX mm', 'ΔY mm', 'ΔZ mm', 'ΔR°', 'ΔP°', 'ΔY°']
        : ['X mm', 'Y mm', 'Z mm', 'R°', 'P°', 'Y°'];
    const ids = ['th-x', 'th-y', 'th-z', 'th-r', 'th-p', 'th-yaw'];
    ids.forEach((id, i) => {
        const el = document.getElementById(id);
        if (el) el.textContent = labels[i];
    });
}

async function setZeroAll() {
    try {
        const r = await fetch('/api/zero_tags', { method: 'POST', headers: authHeaders() });
        const j = await r.json();
        zeroedIds = new Set((j.zeroed_ids || []).map(Number));
        const status = document.getElementById('zero-status');
        if (zeroedIds.size === 0) {
            if (status) status.textContent = 'no markers visible to zero';
            addLog("Zero failed: no markers currently visible.");
            return;
        }
        if (status) status.textContent = `zeroed: ${[...zeroedIds].join(', ')}`;
        setHeaderMode(true);
        addLog(`Zero set for ${zeroedIds.size} marker(s): ${[...zeroedIds].join(', ')}`);
    } catch (e) {
        addLog(`Zero failed: ${e.message}`);
    }
}

async function clearZero() {
    try {
        await fetch('/api/clear_zero', { method: 'POST', headers: authHeaders() });
        zeroedIds.clear();
        const status = document.getElementById('zero-status');
        if (status) status.textContent = 'no zero set';
        setHeaderMode(false);
        addLog("Zero cleared.");
    } catch (e) {
        addLog(`Clear zero failed: ${e.message}`);
    }
}

// Restart tracking loop when rate input changes
document.addEventListener('DOMContentLoaded', () => {
    const rateInput = document.getElementById('tracking-rate-input');
    if (rateInput) {
        rateInput.addEventListener('change', () => {
            if (activeView === 'camera' && webcamStream) startTrackingLoop();
        });
    }
    // Click small webcam preview → switch to camera tab
    const smallVideo = document.getElementById('webcam-video');
    if (smallVideo) smallVideo.addEventListener('click', () => switchTab('camera'));

    // Zero / clear buttons
    const zeroBtn = document.getElementById('zero-all-btn');
    const clearBtn = document.getElementById('clear-zero-btn');
    if (zeroBtn) zeroBtn.addEventListener('click', setZeroAll);
    if (clearBtn) clearBtn.addEventListener('click', clearZero);

    // Focus marker select
    const focusSel = document.getElementById('focus-marker-select');
    if (focusSel) {
        focusSel.addEventListener('change', (e) => {
            focusMarkerId = e.target.value;
        });
    }
});

// Drag webcam preview
(function initWebcamDrag() {
    const preview = document.getElementById('webcam-preview');
    const header = preview.querySelector('.webcam-header');
    let dragging = false, offsetX = 0, offsetY = 0;

    header.addEventListener('mousedown', (e) => {
        if (e.target.closest('.webcam-actions')) return;
        dragging = true;
        const rect = preview.getBoundingClientRect();
        offsetX = e.clientX - rect.left;
        offsetY = e.clientY - rect.top;
        // Switch to top/left positioning so we can move freely
        preview.style.top = rect.top + 'px';
        preview.style.left = rect.left + 'px';
        preview.style.right = 'auto';
        preview.style.bottom = 'auto';
        e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const w = preview.offsetWidth, h = preview.offsetHeight;
        let x = e.clientX - offsetX, y = e.clientY - offsetY;
        x = Math.max(0, Math.min(window.innerWidth - w, x));
        y = Math.max(0, Math.min(window.innerHeight - h, y));
        preview.style.left = x + 'px';
        preview.style.top = y + 'px';
    });

    window.addEventListener('mouseup', () => { dragging = false; });
})();

// The token also travels in the message body so a connection opened before the
// token was stored still works after a reload.
recordBtn.onclick = () => socket.send(JSON.stringify({ action: "start_recording", token: getDashboardToken() }));
stopBtn.onclick = () => socket.send(JSON.stringify({ action: "stop_recording", token: getDashboardToken() }));
updateConfigBtn.onclick = () => socket.send(JSON.stringify({ action: "update_config", save_dir: configSaveDir.value, prefix: configPrefix.value }));
document.getElementById('deploy-btn').onclick = () => { const u = document.getElementById('deploy-user').value, i = deployTargetSelect.value; if (u && i) socket.send(JSON.stringify({ action: "deploy", user: u, ip: i })); };

connect();
startRenderingLoop();