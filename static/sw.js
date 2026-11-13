// 推流 service worker (same as the user's 水与步 app): only shows pushes and opens the app when one is tapped (no offline caching).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(m.title || '推流', {
    body: m.body || '',
    icon: '/static/icon-180.png',
    data: { url: m.url || '/' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL(e.notification.data.url, self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) if ('focus' in c) return c.focus();
    return self.clients.openWindow(url);
  }));
});
