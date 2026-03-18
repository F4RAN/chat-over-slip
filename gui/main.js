const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 700,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile('index.html');
  mainWindow.on('closed', () => { mainWindow = null; });
}

function runCommand(args, { timeout = 60000, input } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(args[0], args.slice(1), {
      stdio: input ? ['pipe', 'pipe', 'pipe'] : ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    proc.stdout?.on('data', (d) => { stdout += d.toString(); });
    proc.stderr?.on('data', (d) => { stderr += d.toString(); });
    const t = setTimeout(() => {
      proc.kill('SIGTERM');
      reject(new Error('Command timed out'));
    }, timeout);
    proc.on('close', (code) => {
      clearTimeout(t);
      resolve({ code, stdout, stderr });
    });
    proc.on('error', (err) => {
      clearTimeout(t);
      reject(err);
    });
    if (input && proc.stdin) proc.stdin.end(input);
  });
}

function buildSshArgs(config, extraOpts = []) {
  const { mode, host, domain, user, password } = config;
  const target = mode === 'ssh' ? host : domain;
  const args = ['sshpass', '-p', password, 'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=45', ...extraOpts];
  if (config.proxyPort) args.push('-o', `ProxyCommand=nc 127.0.0.1 ${config.proxyPort}`);
  args.push(`${user}@${target}`);
  return args;
}

ipcMain.handle('chat:read', async (_, config, limit = 200) => {
  const script = config.remoteScript || '~/chat-over-dnstt/chat.sh';
  const remoteCmd = `bash ${script} -r ${limit}`;
  const args = buildSshArgs(config);
  args.push('bash', '-lc', remoteCmd);
  const { code, stdout } = await runCommand(args);
  return code === 0 ? stdout : null;
});

ipcMain.handle('chat:send', async (_, config, name, text) => {
  const script = config.remoteScript || '~/chat-over-dnstt/chat.sh';
  const msgId = require('crypto').randomBytes(16).toString('hex');
  const remoteCmd = `bash ${script} -n ${JSON.stringify(name)} ${JSON.stringify(msgId)} ${JSON.stringify(text)}`;
  const args = buildSshArgs(config);
  args.push('bash', '-lc', remoteCmd);
  const { code } = await runCommand(args);
  return code === 0;
});

ipcMain.handle('chat:news', async (_, config, channel, rangeSpec) => {
  const script = config.remoteScript || '~/chat-over-dnstt/chat.sh';
  const remoteCmd = `bash ${script} -g ${JSON.stringify(channel)} ${JSON.stringify(rangeSpec || '10')}`;
  const args = buildSshArgs(config, ['-o', 'ConnectTimeout=180']);
  args.push('bash', '-lc', remoteCmd);
  const { code } = await runCommand(args);
  return code === 0;
});

ipcMain.handle('notify:sound', async () => {
  const plat = process.platform;
  if (plat === 'darwin') {
    await runCommand(['afplay', '/System/Library/Sounds/Glass.aiff'], { timeout: 2000 }).catch(() => {});
  } else if (plat === 'linux') {
    await runCommand(['paplay', '/usr/share/sounds/freedesktop/stereo/message.oga'], { timeout: 2000 }).catch(() => {});
  }
});

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
