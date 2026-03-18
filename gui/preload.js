const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('chat', {
  read: (config, limit) => ipcRenderer.invoke('chat:read', config, limit),
  send: (config, name, text) => ipcRenderer.invoke('chat:send', config, name, text),
  news: (config, channel, rangeSpec) => ipcRenderer.invoke('chat:news', config, channel, rangeSpec),
});

contextBridge.exposeInMainWorld('notify', {
  sound: () => ipcRenderer.invoke('notify:sound'),
});
