// Post Automation extension — har Chrome profile mein ek copy (role.js batata hai kaun si).
// Yeh Python server (server.py) se ws://127.0.0.1:8765 par judti hai aur us ke commands chalati hai.
importScripts('role.js');  // const ROLE = 'designer' | 'waseem' | 'wajid' | 'instagram' | 'envato'

const WS_URL = 'ws://127.0.0.1:8765';
const HEALTH_URL = 'http://127.0.0.1:8765/health';
let ws = null;
let connecting = false;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function setStatus(status) { await chrome.storage.session.set({ status }); }
async function addLog(line) {
  const { logs = [] } = await chrome.storage.session.get('logs');
  logs.push(line);
  await chrome.storage.session.set({ logs: logs.slice(-300) });
}

async function connect() {
  if (connecting || (ws && (ws.readyState === 0 || ws.readyState === 1))) return;
  connecting = true;
  try {
    // pehle chupke se dekho server chal raha hai ya nahi (warna WebSocket errors bharte hain)
    const r = await fetch(HEALTH_URL, { cache: 'no-store' }).catch(() => null);
    if (!r || !r.ok) return;
    ws = new WebSocket(WS_URL);
  } catch (e) { return; } finally { connecting = false; }
  if (!ws) return;
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'hello', role: ROLE, version: chrome.runtime.getManifest().version }));
    chrome.storage.session.set({ status: 'connected', can_stop: false });
  };
  ws.onclose = () => { ws = null; setStatus('disconnected'); };
  ws.onerror = () => {};
  ws.onmessage = async ev => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'log') return addLog(msg.line);
    if (msg.type === 'busy') return chrome.storage.session.set({ busy: !!msg.busy,
      paused: !!msg.paused, stopping: !!msg.stopping, can_stop: !!msg.can_stop });
    if (msg.type === 'notify') {  // Windows popup: error / intezar / kaam khatam
      chrome.notifications.create({ type: 'basic', iconUrl: 'icon.png', title: msg.title || 'Post Automation',
        message: (msg.message || '').slice(0, 400), priority: 2, requireInteraction: !!msg.sticky });
      return;
    }
    if (msg.type !== 'cmd') return;
    let reply;
    try {
      reply = { type: 'result', id: msg.id, ok: true, result: await HANDLERS[msg.cmd](msg.args || {}) };
    } catch (e) {
      reply = { type: 'result', id: msg.id, ok: false, error: String((e && e.message) || e) };
    }
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(reply));
  };
}

// service worker ko zinda rakho + server se dobara judo
setInterval(() => {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'ping' }));
  else connect();
}, 20000);
chrome.alarms.create('keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// Gmail watch list: "Edit with Google Docs" nayi tab kholta hai — script ke click par Chrome popup rok deta hai.
// Sirf Designer profile + sirf mail.google.com ke liye popups allow (baqi websites par koi asar nahi).
if (ROLE === 'designer' && chrome.contentSettings && chrome.contentSettings.popups) {
  chrome.contentSettings.popups.set({ primaryPattern: 'https://mail.google.com/*', setting: 'allow' }, () => void chrome.runtime.lastError);
}

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (msg.type === 'start' || msg.type === 'stop') {
    connect();
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify({ type: msg.type })); respond({ ok: true }); }
    else respond({ ok: false, error: 'Server se connection nahi — pehle START_SERVER.bat chalao' });
  } else if (msg.type === 'reconnect') { connect(); respond({ ok: true }); }
  return true;
});

// ------------------------------------------------------------ helpers ----
function waitComplete(tabId, timeout = 90000) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const iv = setInterval(async () => {
      try {
        const t = await chrome.tabs.get(tabId);
        if (t.status === 'complete') { clearInterval(iv); resolve(t); }
        else if (Date.now() - t0 > timeout) { clearInterval(iv); reject(new Error('page load timeout')); }
      } catch (e) { clearInterval(iv); reject(e); }
    }, 500);
  });
}

async function execMain(tabId, func, args = []) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func, args });
  if (r && r.error) throw new Error(String(r.error.message || r.error));
  return r ? r.result : null;
}

// ---- script ka apna window + tab group: aap ki tabs ko kabhi nahi chhuta ----
const GROUP_TITLE = '🤖 Post Automation';

async function autoGroup() {
  const gs = await chrome.tabGroups.query({ title: GROUP_TITLE });
  return gs[0] || null;
}

async function inAutoGroup(tab) {
  const g = await autoGroup();
  return !!(g && tab.groupId === g.id);
}

// nayi tab HAMESHA script ke window/group mein (aap ki window mein tab nahi badalti)
async function newAutoTab(url) {
  const g = await autoGroup();
  let t;
  if (g) {
    t = await chrome.tabs.create({ url, windowId: g.windowId, active: true });
    await chrome.tabs.group({ tabIds: [t.id], groupId: g.id });
  } else {
    const w = await chrome.windows.create({ url, focused: false, state: 'normal' });
    t = w.tabs[0];
    const gid = await chrome.tabs.group({ tabIds: [t.id], createProperties: { windowId: w.id } });
    await chrome.tabGroups.update(gid, { title: GROUP_TITLE, color: 'green' });
  }
  await chrome.tabs.update(t.id, { autoDiscardable: false });
  return t;
}

// Memory Saver / freeze se bachao. Sirf script ki tab samne laata hai (aap ki tabs nahi)
async function keepAwake(tabId) {
  let t = await chrome.tabs.get(tabId);
  if (t.autoDiscardable) await chrome.tabs.update(tabId, { autoDiscardable: false });
  if (t.discarded) { await chrome.tabs.reload(tabId); await waitComplete(tabId); t = await chrome.tabs.get(tabId); }
  if (!t.active && await inAutoGroup(t)) await chrome.tabs.update(tabId, { active: true });
}

function toB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// ------------------------------------------- commands from server.py ----
const HANDLERS = {
  reload() { setTimeout(() => chrome.runtime.reload(), 300); return true; },  // server naya code bheje to
  async list_doc_tabs() {
    const tabs = await chrome.tabs.query({ url: 'https://docs.google.com/document/*' });
    tabs.sort((a, b) => a.windowId - b.windowId || a.index - b.index);
    return tabs.map(t => ({ tabId: t.id, url: t.url, title: t.title }));
  },
  async focus_tab({ tabId }) {
    // sirf script ke group ki tab samne (aap ki window / tabs nahi badalti)
    const t = await chrome.tabs.get(tabId);
    if (await inAutoGroup(t)) await chrome.tabs.update(tabId, { active: true });
    return true;
  },
  async tab_info({ tabId }) {
    const t = await chrome.tabs.get(tabId);
    return { url: t.url, title: t.title, status: t.status };
  },
  async open_tab({ url, active = true }) {
    const t = await newAutoTab(url);
    await waitComplete(t.id);
    return t.id;
  },
  async navigate({ tabId, url }) {
    await chrome.tabs.update(tabId, { url, active: true });
    await sleep(800);
    await waitComplete(tabId);
    return true;
  },
  async close_tab({ tabId }) { await chrome.tabs.remove(tabId); return true; },
  async gmail_tab({ url }) {
    // Gmail watch list: script ke group wali apni Gmail tab (aap ki Gmail tab kabhi use nahi hoti)
    const g = await autoGroup();
    const tabs = g ? (await chrome.tabs.query({ url: 'https://mail.google.com/*' })).filter(x => x.groupId === g.id) : [];
    let t = tabs.find(x => !x.discarded) || tabs[0];
    if (!t) { t = await newAutoTab(url); await waitComplete(t.id); }
    return t.id;
  },
  async goto_quiet({ tabId, url }) {
    // tab ko samne laaye baghair URL badlo (Gmail search)
    await chrome.tabs.update(tabId, { url });
    await sleep(800);
    await waitComplete(tabId);
    return true;
  },
  async fresh_chat_tab({ url = 'https://chatgpt.com/' }) {
    // script ke group mein nayi ChatGPT tab; purani SIRF script wali ChatGPT tabs band (aap ki nahi)
    const g = await autoGroup();
    const old = g ? (await chrome.tabs.query({ url: 'https://chatgpt.com/*' })).filter(x => x.groupId === g.id) : [];
    const t = await newAutoTab(url);
    for (const o of old) { try { await chrome.tabs.remove(o.id); } catch (e) {} }
    await waitComplete(t.id);
    return t.id;
  },
  async chat_tab() {
    // script ke group wali ChatGPT tab (na ho to nayi) — aap ki ChatGPT tab kabhi use nahi hoti
    const g = await autoGroup();
    const tabs = g ? (await chrome.tabs.query({ url: 'https://chatgpt.com/*' })).filter(x => x.groupId === g.id) : [];
    let t = tabs.find(x => !x.discarded) || tabs[0];
    if (!t) { t = await newAutoTab('https://chatgpt.com/'); await waitComplete(t.id); }
    await keepAwake(t.id);
    return t.id;
  },
  async fetch({ url }) {
    const r = await fetch(url, { credentials: 'include' });
    return { status: r.status, contentType: r.headers.get('content-type') || '', b64: toB64(await r.arrayBuffer()) };
  },
  async exec({ tabId, fn, args = [] }) {
    if (!PAGE[fn]) throw new Error('extension mein function nahi: ' + fn + ' (extension reload karo)');
    await keepAwake(tabId);
    return execMain(tabId, PAGE[fn], args);
  },
  async capture({ tabId }) {
    const t = await chrome.tabs.get(tabId);
    await chrome.tabs.update(tabId, { active: true });  // script ki window mein (aap ki window nahi)
    const w = await chrome.windows.get(t.windowId);
    if (w.state === 'minimized') await chrome.windows.update(t.windowId, { state: 'normal' });
    await sleep(500);
    return chrome.tabs.captureVisibleTab(t.windowId, { format: 'png' });
  },
  now() { return new Date().toISOString(); },
  async save_image({ tabId, timeout = 60000 }) {
    // "Right click -> Save image as" jaisa: page par sab se bari nazar aane wali image download karo
    const imgs = (await execMain(tabId, PAGE.main_images)) || [];
    const best = imgs.find(u => /^https?:/.test(u) && !/og|logo|avatar|icon/i.test(u.split('?')[0].split('/').pop()))
      || imgs.find(u => /^https?:/.test(u));
    if (!best) throw new Error('page par koi image nahi mili');
    const id = await chrome.downloads.download({ url: best, conflictAction: 'uniquify', saveAs: false });
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      const [d] = await chrome.downloads.search({ id });
      if (d && d.state === 'complete') return d.filename;
      if (d && d.state === 'interrupted') throw new Error('download ruk gaya: ' + d.error);
      await sleep(800);
    }
    throw new Error('save image timeout');
  },
  async wait_download({ since, timeout = 90000 }) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      const items = await chrome.downloads.search({ startedAfter: since, state: 'complete', orderBy: ['-startTime'] });
      if (items.length) return items[0].filename;
      await sleep(1000);
    }
    throw new Error('download timeout');
  },
};

// ---------------------- functions jo web page ke andar chalti hain ----
// (har function self-contained hona chahiye — page mein copy ho kar chalta hai)
const PAGE = {
  api: async function (path, method, body) {
    // login token 10 min yaad (har request se pehle extra session request nahi)
    const c = window.__paToken;
    let token = c && Date.now() - c.at < 600000 ? c.token : null;
    if (!token) {
      const s = await (await fetch('/api/auth/session')).json();
      token = s.accessToken;
      window.__paToken = { token, at: Date.now() };
    }
    const r = await fetch(path, {
      method, body: body ? JSON.stringify(body) : undefined,
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    });
    if (r.status === 401) window.__paToken = null;  // token purana -> agli dafa naya
    return { status: r.status, text: await r.text() };
  },
  fetch_b64: async function (url) {
    const same = url.startsWith('/') || url.startsWith(location.origin);
    const headers = {};
    if (same) {  // blob: links ko token nahi chahiye; same-site par token 10 min yaad
      const c = window.__paToken;
      let token = c && Date.now() - c.at < 600000 ? c.token : null;
      if (!token) {
        const s = await (await fetch('/api/auth/session')).json();
        token = s.accessToken;
        window.__paToken = { token, at: Date.now() };
      }
      headers['Authorization'] = 'Bearer ' + token;
    }
    const r = await fetch(url, { headers });
    const bytes = new Uint8Array(await r.arrayBuffer());
    let str = '';
    for (let i = 0; i < bytes.length; i += 0x8000) str += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return { status: r.status, contentType: r.headers.get('content-type') || '', b64: btoa(str) };
  },
  fetch_text: async function (url) {
    const r = await fetch(url, { credentials: 'include' });
    return { status: r.status, contentType: r.headers.get('content-type') || '', text: await r.text() };
  },
  has: function (sel) { return !!document.querySelector(sel); },
  turn_images: function () {
    // Generated galleries can be siblings of the assistant message, not descendants.
    // Match the latest user boundary independently of ChatGPT's turn wrapper version.
    const main = document.querySelector('main') || document.body;
    const turns = main.querySelectorAll('[data-turn-key], [data-testid^="conversation-turn"], article[data-turn]');
    if (turns.length) turns[turns.length - 1].scrollIntoView({ block: 'end' });
    const userSel = '[data-message-author-role="user"], [data-turn="user"], [class~="user-message"], [data-chatgpt-search-unit-key$=":user"]';
    const users = [...main.querySelectorAll(userSel)];
    const lastUser = users[users.length - 1];
    if (!lastUser) return { imgs: [], boundary_found: false };
    const imgs = [];
    for (const i of main.querySelectorAll('img')) {
      if (!(lastUser.compareDocumentPosition(i) & 4) || i.closest(userSel)) continue;
      const src = i.currentSrc || i.src || '', alt = i.alt || '';
      if (!src || src.startsWith('data:') || /upload|user attachment/i.test(alt)) continue;
      const gallery = i.closest('[data-testid="generated-image-gallery"], [data-testid="generated-image-preview"]');
      const generated = gallery || /generated/i.test(alt) ||
        (i.closest('[data-message-author-role="assistant"], [data-turn="assistant"]') &&
         /estuary|oaiusercontent|file[_-]/i.test(src) && i.naturalWidth >= 300);
      // Placeholders are not ready to download. Scrolling asks lazy galleries to load.
      if (generated) i.scrollIntoView({ block: 'nearest' });
      if (generated && i.complete && i.naturalWidth > 0 && !imgs.includes(src)) imgs.push(src);
    }
    return { imgs, boundary_found: true };
  },
  inspect_last: function (sel, n) {
    // jaanch: aakhri n elements ke upar ke parents (tag/class/data-*) — page ka dhancha samajhne ke liye
    const els = [...document.querySelectorAll(sel)].slice(-(n || 3));
    return els.map(el => {
      const chain = [];
      for (let p = el, i = 0; p && p !== document.body && i < 14; p = p.parentElement, i++) {
        const data = [...p.attributes].filter(a => a.name.startsWith('data-') || a.name === 'role' || a.name === 'id')
          .map(a => a.name + '=' + String(a.value).slice(0, 40)).join(' ');
        chain.push(p.tagName + (data ? ' [' + data + ']' : '') + ' .' + String(p.className || '').slice(0, 50));
      }
      return { alt: el.alt || '', chain };
    });
  },
  page_text_tail: function (n) {
    // page ka aakhri text (ChatGPT ka jawab) — API request ke baghair
    const t = (document.querySelector('main') || document.body).innerText || '';
    return t.slice(-(n || 4000));
  },
  page_images: function () {
    // chat mein nazar aane wali images (API ke baghair) — generated vs user upload pehchanne ke liye info
    const main = document.querySelector('main') || document.body;
    return [...main.querySelectorAll('img')].map(i => {
      const roleEl = i.closest('[data-message-author-role]');
      const turn = i.closest('[data-testid^="conversation-turn"], article, [data-turn]');
      return {
        src: i.currentSrc || i.src || '', alt: i.alt || '',
        w: i.naturalWidth || 0, h: i.naturalHeight || 0,
        shown: Math.round(i.getBoundingClientRect().width),
        role: roleEl ? roleEl.getAttribute('data-message-author-role') : (turn ? (turn.getAttribute('data-turn') || '') : ''),
        turn: turn ? (turn.getAttribute('data-testid') || turn.tagName) : '',
      };
    }).filter(x => x.src && !x.src.startsWith('data:'));
  },
  sidebar_chats: async function (maxScroll) {
    // sidebar se chat titles + id (API request nahi -> 429 nahi). Neeche scroll kar ke purani chats bhi load
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const collect = () => [...document.querySelectorAll('a[href*="/c/"]')].map(a => ({
      id: ((a.getAttribute('href') || '').match(/\/c\/([0-9a-f-]{20,})/) || [])[1],
      title: (a.innerText || a.getAttribute('aria-label') || '').trim().split('\n')[0] })).filter(x => x.id && x.title);
    const seen = new Map();
    const first = document.querySelector('a[href*="/c/"]');
    let scroller = first && first.parentElement;
    while (scroller && scroller !== document.body && scroller.scrollHeight <= scroller.clientHeight + 5) scroller = scroller.parentElement;
    let still = 0;
    for (let i = 0; i < (maxScroll || 40); i++) {
      const before = seen.size;
      for (const c of collect()) if (!seen.has(c.id)) seen.set(c.id, c);
      if (!scroller || scroller === document.body) break;
      scroller.scrollTop = scroller.scrollHeight;
      await sleep(800);
      still = seen.size === before ? still + 1 : 0;
      if (still >= 3) break;
    }
    return [...seen.values()];
  },
  last_assistant_text: function () {
    const els = document.querySelectorAll('[data-message-author-role="assistant"]');
    return els.length ? els[els.length - 1].innerText : '';
  },
  // ---- aam auzaar: ChatGPT ka design badle to server se hi theek ho jaye (reload ki zaroorat nahi) ----
  query: function (sel) {
    return [...document.querySelectorAll(sel)].slice(0, 60).map(e => ({
      tag: e.tagName, id: e.id, testid: e.getAttribute('data-testid'), aria: e.getAttribute('aria-label'),
      text: (e.innerText || '').trim().slice(0, 40), disabled: !!e.disabled, visible: !!e.offsetParent }));
  },
  click_text: function (sel, text) {
    // dikhne wala element jiska text hoobahoo yeh ho (jaise ChatGPT ka "Chat" / "Work" toggle)
    const e = [...document.querySelectorAll(sel)].find(x => x.offsetParent && (x.innerText || '').trim() === text);
    if (!e) return false;
    e.click();
    return true;
  },
  click: function (sel, i) {
    const els = [...document.querySelectorAll(sel)].filter(e => e.offsetParent);
    const e = els[i || 0];
    if (!e) return false;
    e.click();
    return true;
  },
  compose_buttons: function (sel) {
    // message box ke aas paas ke buttons (send / stop yahin hote hain)
    const comp = document.querySelector(sel.composer);
    if (!comp) return [];
    let box = comp.closest('form');
    if (!box) { box = comp; for (let i = 0; i < 6 && box.parentElement; i++) box = box.parentElement; }
    return [...box.querySelectorAll('button')].map(e => ({ id: e.id, testid: e.getAttribute('data-testid'),
      aria: e.getAttribute('aria-label'), disabled: !!e.disabled, visible: !!e.offsetParent }));
  },
  compose: async function (text, files, sel) {
    // files lagao + text likho (BHEJO NAHI)
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let comp = document.querySelector(sel.composer);
    if (!comp) throw new Error('ChatGPT ka message box nahi mila');
    let attached = 0;
    if (files && files.length) {
      const dt = new DataTransfer();
      for (const f of files) {
        const bin = atob(f.b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        dt.items.add(new File([arr], f.name, { type: f.type }));
      }
      // message box ka hissa (form ya us ke upar ke 6 parents) — sirf yahan ke thumbnails gino
      const boxOf = () => {
        const c = document.querySelector(sel.composer);
        if (!c) return null;
        let b = c.closest('form');
        if (!b) { b = c; for (let i = 0; i < 6 && b.parentElement; i++) b = b.parentElement; }
        return b;
      };
      const thumbs = () => {
        const b = boxOf();
        return b ? [...b.querySelectorAll('img')].filter(i =>
          !i.closest('[data-turn-key], [data-testid^="conversation-turn"], [data-message-author-role], [data-turn], [data-testid="generated-image-gallery"]') &&
          !/generated|avatar|profile/i.test(i.alt || '') &&
          /^(blob:|data:image)|estuary|oaiusercontent|file[_-]/i.test(i.currentSrc || i.src || '')).length : 0;
      };
      const base = thumbs();
      if (base) throw new Error('Composer mein purani attachments hain — chat dobara kholo');
      const waitThumbs = async ms => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) { if (thumbs() >= files.length) return thumbs() === files.length; await sleep(1000); }
        return false;
      };
      let ok = false;
      // 1) file input  2) paste (Ctrl+V jaisa)  3) drag-drop — jab tak thumbnail na dikhe
      const inputs = [...document.querySelectorAll(sel.file_input)];
      const inp = inputs.find(i => /image/.test(i.accept || '') && !/video/.test(i.accept || '')) ||
                  inputs.find(i => !i.accept || /image|\*/.test(i.accept)) || inputs[0];
      if (inp) {
        inp.files = dt.files;
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        ok = await waitThumbs(25000);
      }
      if (!ok && thumbs() === 0) {
        comp = document.querySelector(sel.composer) || comp;
        comp.focus();
        comp.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        ok = await waitThumbs(20000);
      }
      if (!ok && thumbs() === 0) {
        const tgt = boxOf() || comp;
        for (const type of ['dragenter', 'dragover', 'drop'])
          tgt.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
        ok = await waitThumbs(20000);
      }
      await sleep(3000);
      attached = thumbs();
    }
    // image lagne ke baad ChatGPT box dobara bana deta hai -> naya box dhoondo
    comp = document.querySelector(sel.composer) || comp;
    comp.focus();
    if (comp.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      setter.call(comp, text);
      comp.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      const dt2 = new DataTransfer();
      dt2.setData('text/plain', text);
      comp.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt2, bubbles: true, cancelable: true }));
      await sleep(1500);
      if (!comp.innerText.trim()) { document.execCommand('insertText', false, text); }
    }
    await sleep(1000);
    const now = document.querySelector(sel.composer) || comp;
    return { typed: (now.innerText || now.value || '').trim().length, attached, expected: (files || []).length };
  },
  submit: async function (sel) {
    // send button (uploads khatam hone tak enable ka intezar); na mile to Enter
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const near = () => {
      const comp = document.querySelector(sel.composer);
      let box = comp && (comp.closest('form') || comp.parentElement);
      for (let i = 0; box && i < 6 && !box.querySelector('button'); i++) box = box.parentElement;
      if (!box) return null;
      return [...box.querySelectorAll('button')].find(e => e.offsetParent &&
        /send|submit/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('data-testid') || '') + ' ' + e.id));
    };
    const t0 = Date.now();
    while (Date.now() - t0 < 180000) {
      const b = document.querySelector(sel.send_button) || near();
      if (b && !b.disabled) { await sleep(800); b.click(); return 'button'; }
      const comp = document.querySelector(sel.composer);
      if (!b && Date.now() - t0 > 20000 && comp) {  // koi send button hi nahi -> Enter
        comp.focus();
        comp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        return 'enter';
      }
      await sleep(1000);
    }
    throw new Error('Send button enable nahi hua (upload atka?)');
  },
  send: async function (text, files, sel) {
    // NOTE: function page mein akela chalta hai, is liye compose/submit ka code yahan dobara nahi bula sakte
    throw new Error('send purana hai — server compose + submit use karta hai');
  },
  brand_snapshot: async function () {
    if (document.fonts) await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 8000))]);
    const visible = el => {
      const r = el.getBoundingClientRect(), s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const background = el => {
      for (let n = el; n; n = n.parentElement) {
        const c = getComputedStyle(n).backgroundColor;
        if (c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
      }
      return 'transparent';
    };
    const styles = [];
    const add = (role, selector, count) => {
      for (const el of [...document.querySelectorAll(selector)].filter(e => visible(e) && e.textContent.trim()).slice(0, count)) {
        const s = getComputedStyle(el);
        styles.push({ role, font_family: s.fontFamily, font_weight: s.fontWeight,
          color: s.color, background: background(el), font_size: s.fontSize, letter_spacing: s.letterSpacing });
      }
    };
    add('heading', 'h1, h2', 4);
    add('body', 'main p, article p', 3);
    if (!styles.some(s => s.role === 'body')) add('body', 'p, body', 2);
    add('button', 'button, a.btn, a.button, .elementor-button', 3);
    const logos = [];
    for (const el of document.images) {
      if (!visible(el)) continue;
      const src = el.currentSrc || el.src, label = `${el.alt || ''} ${el.id} ${el.className} ${src}`;
      const header = !!el.closest('header, [role="banner"], .site-header, #masthead');
      const anchor = el.closest('a[href]');
      const home = anchor && new URL(anchor.href, location.href).pathname.replace(/\/+$/, '') === '';
      const score = (/logo/i.test(label) ? 100 : 0) + (header ? 30 : 0) + (home ? 20 : 0);
      if (score < 50 || /payment|visa|mastercard|paypal|trustpilot/i.test(label)) continue;
      logos.push({ src, label: (el.alt || '').slice(0, 150), score, width: el.naturalWidth, height: el.naturalHeight });
    }
    for (const el of document.querySelectorAll('[class*="logo"] svg, [id*="logo"] svg, header a[href="/"] svg')) {
      if (!visible(el)) continue;
      const clone = el.cloneNode(true), original = [el, ...el.querySelectorAll('*')], copies = [clone, ...clone.querySelectorAll('*')];
      original.forEach((n, i) => {
        const s = getComputedStyle(n);
        for (const p of ['fill', 'stroke', 'stroke-width', 'font-family', 'font-weight']) copies[i].style.setProperty(p, s.getPropertyValue(p));
      });
      clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      const r = el.getBoundingClientRect();
      clone.setAttribute('width', r.width); clone.setAttribute('height', r.height);
      const xml = new XMLSerializer().serializeToString(clone);
      if (xml.length < 300000) logos.push({src: 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(xml))),
        label: (el.getAttribute('aria-label') || el.textContent || 'Website header logo').slice(0, 150), score: 120});
    }
    logos.sort((a, b) => b.score - a.score);
    return { url: location.href, title: document.title, site_name: document.querySelector('meta[property="og:site_name"]')?.content || '',
      heading: [...document.querySelectorAll('h1')].map(e => e.textContent.trim()).join(' ').slice(0, 400),
      logo_labels: logos.slice(0, 4).map(l => l.label).join(' '), styles, logos: logos.slice(0, 8),
      fonts_loaded: document.fonts?.status === 'loaded', captured_at: new Date().toISOString() };
  },
  brand_rasterize_svg: async function (encoded) {
    const img = new Image();
    img.src = 'data:image/svg+xml;base64,' + encoded;
    await Promise.race([img.decode(), new Promise((_, reject) => setTimeout(() => reject(new Error('Logo decode timeout')), 10000))]);
    const w = img.naturalWidth || 600, h = img.naturalHeight || 200;
    const scale = Math.min(4, 1600 / Math.max(w, h));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(w * scale)); canvas.height = Math.max(1, Math.round(h * scale));
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/png');
  },
  main_image_rect: async function () {
    // page ki sab se bari dikhne wali image (Envato preview) -> beech mein laao, us ki jagah + asal src
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const big = [...document.images].filter(i => i.naturalWidth > 300 && i.naturalHeight > 300)
      .map(i => ({ i, a: i.getBoundingClientRect().width * i.getBoundingClientRect().height }))
      .sort((a, b) => b.a - a.a)[0];
    if (!big) return null;
    big.i.scrollIntoView({ block: 'center', inline: 'center' });
    await sleep(800);
    const r = big.i.getBoundingClientRect();
    return { x: Math.max(0, r.left), y: Math.max(0, r.top), w: Math.min(r.width, innerWidth - Math.max(0, r.left)),
             h: Math.min(r.height, innerHeight - Math.max(0, r.top)), dpr: devicePixelRatio,
             src: big.i.currentSrc || big.i.src, nw: big.i.naturalWidth, nh: big.i.naturalHeight };
  },
  og_image: function () {
    const m = document.querySelector('meta[property="og:image"]');
    return m ? m.content : null;
  },
  main_images: function () {
    // og:image + page ki sab se bari images (product page / Pinterest)
    const out = [];
    const og = document.querySelector('meta[property="og:image"]');
    if (og && og.content) out.push(og.content);
    // screen par jo sab se bari dikh rahi hai (Google Images preview, product photo, Pinterest pin)
    const imgs = [...document.images]
      .map(i => { const r = i.getBoundingClientRect();
        return { src: i.currentSrc || i.src, a: Math.max(r.width * r.height, 0), n: i.naturalWidth * i.naturalHeight }; })
      .filter(i => i.src && i.n > 200 * 200 && !i.src.startsWith('data:'))
      .sort((a, b) => b.a - a.a);
    const shown = imgs.map(i => i.src);
    return [...shown.slice(0, 5), ...out];
  },
  diag: function () {
    // ChatGPT page ka haal (jab message box na mile)
    const pick = el => ({ tag: el.tagName, id: el.id, testid: el.getAttribute('data-testid'),
      aria: el.getAttribute('aria-label'), ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
      cls: (el.className && el.className.baseVal === undefined ? el.className : '').toString().slice(0, 80) });
    return {
      url: location.href, title: document.title,
      editables: [...document.querySelectorAll('[contenteditable="true"], textarea')].slice(0, 8).map(pick),
      buttons: [...document.querySelectorAll('button[data-testid], button[aria-label]')].slice(0, 40).map(pick),
      fileInputs: [...document.querySelectorAll('input[type="file"]')].map(i => i.accept),
      text: (document.body.innerText || '').slice(0, 400),
    };
  },
  freepik_click: function () {
    const els = [...document.querySelectorAll('button, a')]
      .filter(e => /^\s*download/i.test(e.innerText || '') && e.offsetParent);
    if (!els.length) return false;
    els[els.length > 1 && document.querySelector('[role="dialog"]') ? els.length - 1 : 0].click();
    return true;
  },
  envato_click: function () {
    // pehle popup (dialog) ke andar wala button, warna page ka Download button
    const vis = e => e.offsetParent && !e.disabled;
    const rx = /^\s*(add\s*&\s*download|download\s*without\s*license|download)\b/i;
    const dlg = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')].pop();
    const scope = dlg || document;
    const btns = [...scope.querySelectorAll('button, a')].filter(e => vis(e) && rx.test(e.innerText || e.getAttribute('aria-label') || ''));
    const pref = btns.find(b => /add\s*&\s*download/i.test(b.innerText)) || btns[0];
    if (!pref) return false;
    pref.click();
    return true;
  },
  ig_prepare: function () {
    const texts = ['Allow all cookies', 'Decline optional cookies', 'Not now', 'Not Now'];
    for (const b of document.querySelectorAll('button, div[role="button"]'))
      if (texts.includes((b.innerText || '').trim())) b.click();
    const c = document.querySelector('svg[aria-label="Close"]');
    if (c) (c.closest('button, div[role="button"]') || c).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    const root = document.querySelector('article') || document.querySelector('main');
    if (root) root.scrollIntoView({ block: 'center' });
    return true;
  },
  ig_media: function () {
    // nazar aane wali slide ki image: pehle use screen ke beech laao, phir rect + asal file ka link
    const root = document.querySelector('article') || document.querySelector('main') || document.body;
    const pick = () => {
      let best = null, area = 0;
      for (const el of root.querySelectorAll('img, video')) {
        const b = el.getBoundingClientRect();
        const w = Math.max(Math.min(b.right, innerWidth) - Math.max(b.left, 0), 0);
        const h = Math.max(Math.min(b.bottom, innerHeight) - Math.max(b.top, 0), 0);
        const a = b.width * b.height > 0 ? Math.min(b.width * b.height, (w || b.width) * (b.height)) : 0;
        if (a > area && b.width > 150 && b.height > 150) { area = a; best = el; }
      }
      return best;
    };
    let el = pick();
    if (!el) return null;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const b = el.getBoundingClientRect();
    const x1 = Math.max(b.left, 0), y1 = Math.max(b.top, 0);
    const x2 = Math.min(b.right, innerWidth), y2 = Math.min(b.bottom, innerHeight);
    const next = document.querySelector('button[aria-label="Next"], [aria-label="Next"][role="button"]') ||
      (document.querySelector('svg[aria-label="Next"]') || {}).parentElement;
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1, dpr: devicePixelRatio,
             src: el.tagName === 'IMG' ? (el.currentSrc || el.src) : (el.poster || ''),
             hasNext: !!(next && next.getBoundingClientRect().width) };
  },
  ig_next: function () {
    const n = document.querySelector('button[aria-label="Next"], [aria-label="Next"][role="button"]') ||
      (document.querySelector('svg[aria-label="Next"]') || {}).parentElement;
    if (n) { (n.closest('button, [role="button"]') || n).click(); return true; }
    return false;
  },

  // ---- Gmail watch list (Designer profile): email ki .docx -> "Edit with Google Docs" ----
  gmail_state: function () {
    // search results ki dikhne wali rows (subject + sender + attachment naam), ya "koi email nahi"
    const vis = e => !!(e && e.offsetParent);
    if (/accounts\.google\.com|ServiceLogin/.test(location.href)) return { loggedOut: true, rows: [] };
    const rows = [...document.querySelectorAll('tr.zA')].filter(vis).slice(0, 50).map((r, i) => {
      const idEl = r.querySelector('[data-legacy-thread-id], [data-thread-id]');
      const id = idEl ? (idEl.getAttribute('data-legacy-thread-id') || idEl.getAttribute('data-thread-id')) : 'row' + i;
      return { i, id, text: (r.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400) };
    });
    const empty = [...document.querySelectorAll('td.TC, div.TC')].some(vis);
    return { loggedOut: false, rows, empty, ready: rows.length > 0 || empty };
  },
  gmail_open_row: function (id) {
    const vis = e => !!(e && e.offsetParent);
    const rows = [...document.querySelectorAll('tr.zA')].filter(vis);
    const row = rows.find(r => { const el = r.querySelector('[data-legacy-thread-id], [data-thread-id]');
      return el && (el.getAttribute('data-legacy-thread-id') === id || el.getAttribute('data-thread-id') === id); })
      || rows[Number(String(id).replace('row', ''))];
    if (!row) return false;
    const target = row.querySelector('td.xY.a4W, .y6, .bog') || row;
    for (const t of ['mousedown', 'mouseup', 'click'])
      target.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    return true;
  },
  gmail_attachments: function () {
    // khuli email (thread) ki attachments: naam + "Edit with Google Docs" button hai ya nahi
    const vis = e => !!(e && e.offsetParent);
    const cards = [...document.querySelectorAll('span[download_url], div[download_url]')].filter(vis);
    const out = cards.map((c, i) => {
      const du = c.getAttribute('download_url') || '';
      const parts = du.split(':');
      const name = parts.length > 2 ? parts[1] : ((c.querySelector('.aV3') || c).innerText || '').trim();
      const edit = [...c.querySelectorAll('[aria-label], [data-tooltip]')].some(b =>
        /(edit|open) with google docs/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-tooltip') || '')));
      return { i, name, edit };
    });
    if (out.length) return out;
    // dusra tareeqa: "Download attachment <naam>" buttons
    return [...document.querySelectorAll('[aria-label^="Download attachment"]')].map((b, i) => ({
      i, name: (b.getAttribute('aria-label') || '').replace(/^Download attachment\s*/i, '').trim(), edit: null }));
  },
  gmail_doc_links: function () {
    // khuli email ke andar Drive / Docs file links (gmail_drive_chip ya aam link) -> file id
    const vis = e => !!(e && e.offsetParent);
    const out = [], seen = new Set();
    for (const a of document.querySelectorAll('.a3s a[href], .gmail_chip a[href]')) {
      if (!vis(a)) continue;
      let u = a.getAttribute('href') || '';
      const safe = a.getAttribute('data-saferedirecturl') || '';
      const m0 = /[?&]q=([^&]+)/.exec(safe);
      if (!/google\.com\/(file|document|open)|docs\.google\.com|drive\.google\.com/.test(u) && m0) u = decodeURIComponent(m0[1]);
      const m = /docs\.google\.com\/document\/d\/([\w-]{20,})/.exec(u) || /drive\.google\.com\/file\/d\/([\w-]{20,})/.exec(u)
        || /drive\.google\.com\/open\?id=([\w-]{20,})/.exec(u);
      if (!m || seen.has(m[1])) continue;
      seen.add(m[1]);
      out.push({ id: m[1], name: (a.getAttribute('aria-label') || a.innerText || '').trim().slice(0, 120), url: u });
    }
    return out;
  },
  gmail_edit: function (i) {
    // attachment card par "Edit with Google Docs" (hover wala button) dabao
    const vis = e => !!(e && e.offsetParent);
    const cards = [...document.querySelectorAll('span[download_url], div[download_url]')].filter(vis);
    const card = cards[i];
    if (!card) return 'card nahi mila';
    const fire = (el, types) => types.forEach(t => el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })));
    fire(card, ['mouseover', 'mouseenter', 'mousemove']);
    const btn = [...card.querySelectorAll('[aria-label], [data-tooltip]')].find(b =>
      /(edit|open) with google docs/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-tooltip') || '')));
    if (!btn) return 'Edit with Google Docs button nahi mila';
    fire(btn, ['mouseover', 'mousedown', 'mouseup', 'click']);
    return 'ok';
  },
};
