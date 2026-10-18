const NAMES = { designer: 'DESIGNER (Docs + Freepik)', waseem: 'WASEEM (ChatGPT designs)',
                wajid: 'WAJID (ChatGPT instructions)', instagram: 'INSTAGRAM (screenshots)', envato: 'ENVATO (images)' };
document.getElementById('title').textContent = 'Post Automation — ' + NAMES[ROLE];
const isDesigner = ROLE === 'designer';
document.getElementById(isDesigner ? 'designerOnly' : 'note').classList.remove('hide');
chrome.runtime.sendMessage({ type: 'reconnect' });

const startBtn = document.getElementById('start');
const stopBtn = document.getElementById('stop');
let requestPending = false;
function control(type) {
  if (requestPending) return;
  requestPending = true;
  startBtn.disabled = stopBtn.disabled = true;
  const error = document.getElementById('error');
  error.hidden = true;
  chrome.runtime.sendMessage({ type }, r => {
    requestPending = false;
    const message = chrome.runtime.lastError?.message || (!r?.ok && (r?.error || 'Server se jawab nahi mila.'));
    if (message) { error.textContent = message; error.hidden = false; }
    refresh();
  });
}
startBtn.onclick = () => control('start');
stopBtn.onclick = () => control('stop');

async function refresh() {
  const { status, logs = [], busy, paused, stopping, can_stop } =
    await chrome.storage.session.get(['status', 'logs', 'busy', 'paused', 'stopping', 'can_stop']);
  const el = document.getElementById('status');
  const ok = status === 'connected';
  el.className = 'st ' + (ok ? 'ok' : 'bad');
  el.textContent = ok ? 'Server se connected ✔' : 'Server se connected NAHI — START_SERVER.bat chalao';
  if (isDesigner) {
    startBtn.disabled = requestPending || !ok || (!!busy && !paused);
    startBtn.textContent = paused ? 'START / RESUME' : busy ? 'Chal raha hai...' : 'START';
    stopBtn.disabled = requestPending || !ok || !can_stop || !busy || paused || stopping;
    stopBtn.textContent = stopping ? 'Ruk raha hai...' : 'STOP / PAUSE';
    document.getElementById('runState').textContent = !ok
      ? 'Server se connection nahi. Pause confirm nahi ho sakta.'
      : !can_stop ? 'STOP enable karne ke liye updated server restart karein.'
      : paused ? 'Ruk gaya — ab net band kar sakte hain. Server khula rakhein; START se wahi kaam resume hoga.'
      : stopping ? 'Current browser command khatam hone dein. “Ruk gaya” aane tak net chalta rakhein.'
      : busy ? 'Net band karne se pehle STOP dabayen aur “Ruk gaya” ka intezar karein.'
      : 'Run shuru karne ke liye START. STOP kaam pause karega; server khula rahega.';
    const log = document.getElementById('log');
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
    log.textContent = logs.join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
}
refresh();
setInterval(refresh, 1000);
