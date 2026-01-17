/**
 * Service Worker for Telegram Archive Web Push Notifications.
 * 
 * This enables push notifications even when the browser tab is closed.
 * The service worker runs in the background and handles:
 * - Receiving push messages from the server
 * - Displaying notifications to the user
 * - Handling notification clicks (opening the relevant chat)
 */

const CACHE_NAME = 'telegram-archive-v1';

// Install event - cache essential files
self.addEventListener('install', (event) => {
    console.log('[SW] Installing service worker');
    // Skip waiting to activate immediately
    self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
    console.log('[SW] Activating service worker');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            );
        })
    );
    // Take control of all pages immediately
    self.clients.claim();
});

// Push event - handle incoming push notifications
self.addEventListener('push', (event) => {
    console.log('[SW] Push received');
    
    let payload = {
        title: 'Telegram Archive',
        body: 'New message received',
        icon: '/static/favicon.ico',
        badge: '/static/favicon.ico',
        tag: 'telegram-archive',
        data: {}
    };
    
    try {
        if (event.data) {
            const data = event.data.json();
            payload = {
                title: data.title || payload.title,
                body: data.body || payload.body,
                icon: data.icon || payload.icon,
                badge: payload.badge,
                tag: data.tag || payload.tag,
                data: data.data || {},
                timestamp: data.timestamp ? new Date(data.timestamp).getTime() : Date.now(),
                requireInteraction: false,
                renotify: true,
                silent: false
            };
        }
    } catch (e) {
        console.error('[SW] Failed to parse push payload:', e);
        if (event.data) {
            payload.body = event.data.text();
        }
    }
    
    const options = {
        body: payload.body,
        icon: payload.icon,
        badge: payload.badge,
        tag: payload.tag,
        data: payload.data,
        timestamp: payload.timestamp,
        requireInteraction: payload.requireInteraction,
        renotify: payload.renotify,
        silent: payload.silent,
        vibrate: [200, 100, 200]
    };
    
    event.waitUntil(
        self.registration.showNotification(payload.title, options)
    );
});

// Notification click event - handle user clicking on notification
self.addEventListener('notificationclick', (event) => {
    console.log('[SW] Notification clicked');
    
    const notification = event.notification;
    const data = notification.data || {};
    
    notification.close();
    
    // Determine the URL to open
    let url = '/';
    if (data.url) {
        url = data.url;
    } else if (data.chat_id) {
        url = `/?chat=${data.chat_id}`;
        if (data.message_id) {
            url += `&msg=${data.message_id}`;
        }
    }
    
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                // Check if there's already a window open
                for (const client of windowClients) {
                    // If we find an existing window, focus it and navigate
                    if ('focus' in client) {
                        return client.focus().then(() => {
                            if (client.url !== url && 'navigate' in client) {
                                return client.navigate(url);
                            }
                            // Post message to the client to navigate/highlight
                            client.postMessage({
                                type: 'NOTIFICATION_CLICK',
                                data: data
                            });
                        });
                    }
                }
                // No existing window, open a new one
                if (clients.openWindow) {
                    return clients.openWindow(url);
                }
            })
    );
});

// Handle notification close
self.addEventListener('notificationclose', (event) => {
    console.log('[SW] Notification closed');
});

// Handle messages from the main page
self.addEventListener('message', (event) => {
    console.log('[SW] Message received:', event.data);
    
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
