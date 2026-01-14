/**
 * Telegram Archive - Service Worker
 * Handles push notifications for new messages and real-time updates.
 */

const CACHE_NAME = 'telegram-archive-v5';
const NOTIFICATION_TAG = 'telegram-archive';

// Install event - cache static assets
self.addEventListener('install', (event) => {
    console.log('[SW] Installing service worker...');
    self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
    console.log('[SW] Activating service worker...');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            );
        })
    );
    self.clients.claim();
});

// Push notification event
self.addEventListener('push', (event) => {
    console.log('[SW] Push received:', event);
    
    let data = {
        title: 'Telegram Archive',
        body: 'New message received',
        icon: '/static/favicon.ico',
        badge: '/static/favicon.ico',
        tag: NOTIFICATION_TAG,
        data: {}
    };
    
    if (event.data) {
        try {
            const payload = event.data.json();
            data = {
                ...data,
                ...payload
            };
        } catch (e) {
            data.body = event.data.text();
        }
    }
    
    const options = {
        body: data.body,
        icon: data.icon,
        badge: data.badge,
        tag: data.tag,
        data: data.data,
        vibrate: [100, 50, 100],
        requireInteraction: false,
        actions: [
            { action: 'open', title: 'Open' },
            { action: 'dismiss', title: 'Dismiss' }
        ]
    };
    
    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
    console.log('[SW] Notification clicked:', event);
    
    event.notification.close();
    
    if (event.action === 'dismiss') {
        return;
    }
    
    // Open or focus the app
    const urlToOpen = event.notification.data?.url || '/';
    
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                // Check if there is already a window/tab open
                for (const client of windowClients) {
                    if (client.url.includes(self.location.origin)) {
                        // Focus existing window and navigate
                        client.focus();
                        if (event.notification.data?.chat_id) {
                            client.postMessage({
                                type: 'navigate',
                                chat_id: event.notification.data.chat_id
                            });
                        }
                        return;
                    }
                }
                // Open new window
                return clients.openWindow(urlToOpen);
            })
    );
});

// Message handler for communication with the main app
self.addEventListener('message', (event) => {
    console.log('[SW] Message received:', event.data);
    
    if (event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
