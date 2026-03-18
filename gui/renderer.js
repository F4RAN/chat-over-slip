const chatEl = document.getElementById('chat');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
const configEl = document.getElementById('config');

let config = null;
let displayName = 'user';
let lastSnapshot = '';
let seenIds = new Set();
let pollInterval = null;

function hasRtl(text) {
  if (!text) return false;
  for (const c of text) {
    const code = c.charCodeAt(0);
    if ((code >= 0x0590 && code <= 0x08FF) || (code >= 0xFB50 && code <= 0xFDFF) || (code >= 0xFE70 && code <= 0xFEFF))
      return true;
  }
  return false;
}

function rtlWrap(text) {
  return hasRtl(text) ? '\u2067' + text + '\u2069' : text;
}

function parseFile(text) {
  const m = text.match(/^\[file\]\s+(.+?)::(.+)$/);
  return m ? { name: m[1], path: m[2], display: `[file] ${m[1]} (/download ${m[1]})` } : null;
}

function renderMessage(ts, msgId, user, text, pending) {
  const file = parseFile(text);
  const body = file ? file.display : text;
  const isOwn = user === displayName;
  const div = document.createElement('div');
  div.className = `bubble ${isOwn ? 'own' : 'other'}${pending ? ' pending' : ''}`;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${ts} · ${user}`;
  const bodyEl = document.createElement('div');
  bodyEl.className = 'body';
  bodyEl.textContent = rtlWrap(body);
  if (hasRtl(body)) bodyEl.dir = 'rtl';
  div.appendChild(meta);
  div.appendChild(bodyEl);
  return div;
}

function render(snapshot) {
  const hadPrevious = seenIds.size > 0;
  let shouldPlay = false;
  chatEl.innerHTML = '';
  const lines = (snapshot || '').split('\n');
  for (const line of lines) {
    if (line.includes('|')) {
      const [ts, msgId, user, text] = line.split('|', 4);
      if (ts && msgId && user && text !== undefined) {
        if (!seenIds.has(msgId) && hadPrevious && user !== displayName) shouldPlay = true;
        seenIds.add(msgId);
        chatEl.appendChild(renderMessage(ts, msgId, user, text, false));
      }
    } else if (line.trim()) {
      const p = document.createElement('p');
      p.style.color = '#888';
      p.textContent = line;
      chatEl.appendChild(p);
    }
  }
  if (shouldPlay && window.notify?.sound) window.notify.sound();
}

async function poll() {
  if (!config) return;
  try {
    const out = await window.chat.read(config, 200);
    if (out !== null && out !== lastSnapshot) {
      lastSnapshot = out;
      render(out);
      statusEl.textContent = 'Online';
      statusEl.style.color = '#4caf50';
    }
  } catch (e) {
    statusEl.textContent = 'Error: ' + (e.message || 'disconnected');
    statusEl.style.color = '#f44336';
  }
}

function send() {
  const text = inputEl.value.trim();
  if (!text || !config) return;
  if (text === '/clear') { inputEl.value = ''; return; }
  inputEl.value = '';
  if (text.startsWith('/news ')) {
    const parts = text.slice(6).trim().split(/\s+/);
    const channel = parts[0];
    const range = parts[1] || '10';
    window.chat.news(config, channel, range).then(() => poll()).catch(() => {});
    return;
  }
  chatEl.appendChild(renderMessage(
    new Date().toISOString().slice(0, 19).replace('T', ' '),
    'pending-' + Date.now(),
    displayName,
    text,
    true
  ));
  chatEl.scrollTop = chatEl.scrollHeight;
  window.chat.send(config, displayName, text).then(() => poll()).catch(() => {});
}

sendBtn.addEventListener('click', send);
inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

configEl.style.display = 'block';
document.getElementById('connect').addEventListener('click', () => {
  config = {
    mode: document.getElementById('mode').value || 'ssh',
    host: document.getElementById('host').value,
    domain: document.getElementById('domain').value,
    user: document.getElementById('user').value,
    password: document.getElementById('pass').value,
    remoteScript: '~/chat-over-dnstt/chat.sh',
  };
  displayName = config.user;
  if (config.mode === 'dns') config.proxyPort = 8000;
  configEl.style.display = 'none';
  pollInterval = setInterval(poll, 3000);
  poll();
});
