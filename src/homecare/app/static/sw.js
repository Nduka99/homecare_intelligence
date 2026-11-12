"use strict";
/* The carer app's service worker: keeps the carer's day sheet and note pages for when there is
   no signal.

   Network first: a page is always fetched fresh when there is signal, and the saved copy is
   used only without it. A page saved on an earlier day is never shown. Only the carer's own
   pages and the app's static files are saved; every other request goes straight to the network.

   Pages are saved under a canonical address: one-time parameters such as `sent=1` are dropped,
   so a sheet opened after a Send is still found at `/today`, and a page that carries a one-time
   message is never saved (a clean copy is fetched instead). The app script asks the worker to
   save the day's pages once it is in control, so the first open of the day is covered too.
   Signing out deletes the saved copies (Clear-Site-Data, and the app script). */
const worker = self;
const CACHE = "carer-pages-1";
const SHELL = [
    "/static/app.css",
    "/static/app.js",
    "/static/manifest.webmanifest",
    "/static/icon-192.png",
    "/static/icon-512.png",
];
const PAGES = [/^\/today$/, /^\/visits\/[^/]+\/note$/];
const ONE_TIME = ["sent"]; // query parameters that carry a message for one load only
/* The address a page is saved under: the one-time parameters removed. */
function canonical(url) {
    const clean = new URL(url.href);
    for (const name of ONE_TIME) {
        clean.searchParams.delete(name);
    }
    return clean.href;
}
function savedToday(response) {
    const date = response.headers.get("date");
    return date !== null && new Date(date).toDateString() === new Date().toDateString();
}
function isPage(url) {
    return PAGES.some((pattern) => pattern.test(url.pathname));
}
/* Save one page under its canonical address. A page with a one-time message is fetched clean
   first, so the saved copy never shows "Your note was sent" on a later open. A redirect is never
   followed: after sign-out it leads to the sign-in page, and asking for that page in the
   background starts a second session that replaces the one the form on screen belongs to. */
async function savePage(url) {
    const cache = await caches.open(CACHE);
    const address = canonical(url);
    const response = await fetch(address, { credentials: "same-origin", redirect: "manual" });
    if (response.ok && response.type === "basic") {
        await cache.put(address, response);
    }
}
async function networkFirst(request) {
    const url = new URL(request.url);
    const page = isPage(url);
    const cache = await caches.open(CACHE);
    try {
        const response = await fetch(request);
        // A redirect (to sign in) or an error is passed on, never saved.
        if (response.ok && response.type === "basic" && !response.redirected) {
            if (page && canonical(url) !== url.href) {
                // A one-time message is on this page: save a clean copy instead, in the background.
                void savePage(url);
            }
            else {
                await cache.put(page ? canonical(url) : request, response.clone());
            }
        }
        return response;
    }
    catch {
        const kept = await cache.match(page ? canonical(url) : request);
        if (kept && (!page || savedToday(kept))) {
            return kept;
        }
        return new Response("No signal, and this page is not saved on this phone.", {
            status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
    }
}
async function removeOldCaches() {
    for (const name of await caches.keys()) {
        if (name !== CACHE) {
            await caches.delete(name);
        }
    }
}
worker.addEventListener("install", (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
    void worker.skipWaiting();
});
worker.addEventListener("activate", (event) => {
    event.waitUntil(removeOldCaches().then(() => worker.clients.claim()));
});
worker.addEventListener("fetch", (event) => {
    const url = new URL(event.request.url);
    if (event.request.method !== "GET" || url.origin !== worker.location.origin) {
        return;
    }
    if (isPage(url) || url.pathname.startsWith("/static/")) {
        event.respondWith(networkFirst(event.request));
    }
});
/* The app script sends the day's page addresses once the worker is in control. Only the
   carer's own page kinds are accepted, whatever the message says. */
worker.addEventListener("message", (event) => {
    const data = event.data;
    if (typeof data !== "object" || data === null || !("save" in data)) {
        return;
    }
    const wanted = data.save;
    if (!Array.isArray(wanted)) {
        return;
    }
    for (const item of wanted) {
        if (typeof item !== "string") {
            continue;
        }
        const url = new URL(item, worker.location.origin);
        if (url.origin === worker.location.origin && isPage(url)) {
            void savePage(url).catch(() => undefined);
        }
    }
});
