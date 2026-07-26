// Minimal service worker: just enough to make the app installable.
// No offline caching of app data (attendance requires a live server
// round-trip anyway -- there's nothing meaningful to cache offline).
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => self.clients.claim());
