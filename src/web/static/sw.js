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
    
    // `url` is relative; client.url is the absolute creation URL, so comparing them
    // directly was true every time and client.navigate() always ran — a full document
    // reload that threw away the loaded messages, the scroll position, any open
    // lightbox, and (the audio engine is one in-page element) whatever was playing.
    const targetUrl = new URL(url, self.location.origin).href;
    const isSameOrigin = (client) => {
        try {
            return new URL(client.url).origin === self.location.origin;
        } catch (e) {
            return false;
        }
    };

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                // Only our own pages can act on the message, and only they should be
                // focused for this notification.
                const ownClients = windowClients.filter(isSameOrigin);
                // Prefer handing the click to an open tab: focus it and post the deep
                // link so the page navigates in place. The tab is selected on 'focus'
                // — NOT on 'postMessage', which exists on every window client per
                // spec, so that predicate matched unconditionally and made the
                // openWindow fallback below unreachable.
                const focusable = ownClients.find((client) => 'focus' in client);
                if (focusable) {
                    return Promise.resolve()
                        .then(() => focusable.focus())
                        .then((focused) => {
                            (focused || focusable).postMessage({
                                type: 'NOTIFICATION_CLICK',
                                data: data
                            });
                        })
                        .catch((err) => {
                            // The tab refused focus or died mid-click: land the click
                            // in a fresh window instead of swallowing it. An unhandled
                            // rejection here would reject waitUntil and kill the whole
                            // click. Log the failure type only, never the deep link.
                            console.error('[SW] Failed to hand the click to an open tab:', err && err.name);
                            if (clients.openWindow) {
                                return clients.openWindow(targetUrl);
                            }
                        });
                }
                // No focusable window of ours: open one directly on the deep link so
                // a cold start still lands on the right chat.
                if (clients.openWindow) {
                    return clients.openWindow(targetUrl);
                }
            })
    );
});

// Handle notification close
self.addEventListener('notificationclose', (event) => {
    console.log('[SW] Notification closed');
});

// Handle push subscription expiry/renewal (auto-resubscribe)
self.addEventListener('pushsubscriptionchange', (event) => {
    console.log('[SW] Push subscription changed, re-subscribing...');
    event.waitUntil(
        self.registration.pushManager.subscribe(
            event.oldSubscription ? event.oldSubscription.options : { userVisibleOnly: true }
        ).then((newSub) => {
            return fetch('/api/push/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(newSub.toJSON())
            });
        }).then((response) => {
            if (response.ok) {
                console.log('[SW] Re-subscribed after subscription change');
            } else {
                console.error('[SW] Re-subscribe failed:', response.status);
            }
        }).catch((err) => {
            console.error('[SW] Re-subscribe error:', err);
        })
    );
});

// Handle messages from the main page.
// Nothing about the payload is logged: a message can carry identifiers, and the
// console travels with screen shares, devtools screenshots and browser bug reports.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
