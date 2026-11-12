"use strict";
/* The carer app, loaded only on carer pages.

   It keeps note drafts on the phone (IndexedDB), sends a note only when the carer taps Send,
   and keeps a note that could not go (no signal) until the carer sends it from "Your visits".
   Each draft keeps its idempotency key, so sending it again never stores the note twice.
   Signing out deletes everything the app keeps on the phone.

   It never writes HTML: every text goes in through textContent, and the page's CSP requires
   Trusted Types, so writing markup would throw.

   With signal, it also shows OpenStreetMap's map of the carer's route (Leaflet, self-hosted);
   without, the server's sketch stays. */
const DATABASE = "carer-drafts";
const STORE = "drafts";
const WORKER = "/sw.js";
const LEAFLET = "/static/vendor/leaflet/leaflet.js";
// OSM's tile server, as its usage policy asks; tiles are only ever fetched for the area in view.
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const RECHECK = 10_000; // milliseconds between signal checks while notes wait
const NO_SIGNAL = "No signal: the note is kept on this phone. Send it from Your visits when you have signal.";
/* The page's two Trusted Types policies, made once. "carer-app" allows exactly two scripts: the
   service worker and Leaflet. "default" lets exactly one string be written as HTML, Leaflet's
   load-time test "<svg/>"; for anything else it returns nothing, so the browser blocks the
   write and reports the violation, as it does with no default policy. */
const trusted = globalThis.trustedTypes;
const scripts = trusted?.createPolicy("carer-app", {
    createScriptURL: (url) => {
        if (url !== WORKER && url !== LEAFLET) {
            throw new TypeError(`only ${WORKER} and ${LEAFLET} may run`);
        }
        return url;
    },
});
trusted?.createPolicy("default", {
    createHTML: (html) => (html === "<svg/>" ? html : null),
});
function openDrafts() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DATABASE, 1);
        request.onupgradeneeded = () => {
            request.result.createObjectStore(STORE, { keyPath: "id" });
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}
async function withStore(mode, work) {
    const database = await openDrafts();
    try {
        return await new Promise((resolve, reject) => {
            const request = work(database.transaction(STORE, mode).objectStore(STORE));
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }
    finally {
        database.close();
    }
}
function saveDraft(draft) {
    return withStore("readwrite", (store) => store.put(draft));
}
function readDraft(id) {
    return withStore("readonly", (store) => store.get(id));
}
function deleteDraft(id) {
    return withStore("readwrite", (store) => store.delete(id));
}
function allDrafts() {
    return withStore("readonly", (store) => store.getAll());
}
/* How long ago, in words. A clock time would clash with the demo's own clock, and "12 minutes
   ago" answers the carer's real question: how old is this copy? */
function ago(milliseconds) {
    const minutes = Math.floor((Date.now() - milliseconds) / 60_000);
    if (minutes < 1)
        return "just now";
    if (minutes === 1)
        return "1 minute ago";
    if (minutes < 60)
        return `${minutes} minutes ago`;
    const hours = Math.floor(minutes / 60);
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
}
function csrfToken() {
    return document.querySelector('input[name="csrf"]')?.value ?? "";
}
/* Post a draft as the plain form would. It counts as sent only when the server answers with
   the "Your visits" page; a signed-out carer is sent to the sign-in page instead. */
async function send(draft) {
    const fields = new URLSearchParams({
        csrf: csrfToken(),
        key: draft.key,
        body: draft.body,
        amends: draft.amends,
    });
    const response = await fetch(`/visits/${encodeURIComponent(draft.visit)}/notes`, {
        method: "POST",
        body: fields,
        credentials: "same-origin",
    });
    const landed = new URL(response.url).pathname;
    if (response.ok && landed === "/today") {
        return { outcome: "sent", url: response.url };
    }
    return { outcome: landed === "/signin" ? "signed out" : "refused", url: response.url };
}
function field(form, name) {
    const found = form.elements.namedItem(name);
    if (!(found instanceof HTMLInputElement || found instanceof HTMLTextAreaElement)) {
        throw new Error(`the note form has no ${name} field`);
    }
    return found;
}
async function initWritePage(form) {
    const carer = form.dataset.carer ?? "";
    const visit = form.dataset.visit ?? "";
    const id = `${carer}/${visit}`;
    const body = field(form, "body");
    const key = field(form, "key");
    const amends = field(form, "amends");
    const state = form.querySelector(".draft-state");
    const say = (text) => {
        if (state)
            state.textContent = text;
    };
    const kept = await readDraft(id);
    if (kept?.body.trim() && !body.value) {
        // The kept key and target come back too, so a note that did reach the server is not
        // stored again when it is resent.
        body.value = kept.body;
        key.value = kept.key;
        amends.value = kept.amends;
        say(`Draft kept on this phone, saved ${ago(kept.saved)}.`);
    }
    const draft = (waiting) => ({
        id,
        carer,
        visit,
        amends: amends.value,
        key: key.value,
        body: body.value,
        saved: Date.now(),
        waiting,
    });
    body.addEventListener("input", () => {
        void saveDraft(draft(false)).then(() => say("Draft saved on this phone."));
    });
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        void submit(form, draft(true), say);
    });
}
async function submit(form, draft, say) {
    await saveDraft(draft);
    try {
        const { outcome, url } = await send(draft);
        if (outcome === "sent") {
            await deleteDraft(draft.id);
            window.location.assign(url);
            return;
        }
        if (outcome === "refused") {
            await saveDraft({ ...draft, waiting: false });
        }
        // Let the server show its own answer (a message to fix, or the sign-in page).
        form.submit();
    }
    catch {
        say(NO_SIGNAL);
    }
}
async function initTodayPage(panel) {
    const carer = panel.dataset.carer ?? "";
    const waiting = (await allDrafts()).filter((draft) => draft.carer === carer && draft.waiting);
    if (waiting.length === 0) {
        return;
    }
    // Name each waiting visit as the sheet does, and mark its row, so nobody writes it twice.
    const names = [];
    for (const draft of waiting) {
        const row = document.querySelector(`li[data-visit="${CSS.escape(draft.visit)}"]`);
        const client = row?.querySelector(".what strong")?.textContent ?? "a visit";
        names.push(`visit ${row?.querySelector(".sec")?.textContent ?? "?"} (${client})`);
        const mark = document.createElement("p");
        mark.className = "waiting-mark";
        mark.textContent = "Note waiting on this phone to be sent.";
        row?.querySelector(".note-state")?.after(mark);
        // The note is written, so no link offers to write it: the Send button above is now the one
        // thing to do. Every link to its form, in "To write up" and in the visit's row, becomes a
        // plain link that opens the note to change it before sending.
        const links = document.querySelectorAll(`main a[href="/visits/${CSS.escape(draft.visit)}/note"]`);
        for (const link of links) {
            if (link.closest(".write-up")) {
                link.before(mark.cloneNode(true));
            }
            link.className = "";
            link.textContent = "Open the note";
        }
    }
    const text = document.createElement("p");
    text.textContent = `Waiting on this phone to be sent: ${names.join(", ")}.`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button primary";
    const reason = document.createElement("p");
    reason.className = "draft-state";
    const ready = (online) => {
        button.disabled = !online;
        button.textContent = online ? "Send now" : "Send when there is signal";
        reason.textContent = online ? "" : "No signal yet: the notes stay safe on this phone.";
    };
    ready(await connected());
    // Signal often returns with no "online" event (a phone on weak data), so ask again.
    window.setInterval(() => void showConnection(), RECHECK);
    document.addEventListener("connection", (event) => {
        ready(event.detail);
    });
    button.addEventListener("click", () => {
        void sendWaiting(waiting, text, button);
    });
    panel.append(text, button, reason);
    panel.hidden = false;
}
async function sendWaiting(waiting, text, button) {
    button.disabled = true;
    let left = 0;
    for (const draft of waiting) {
        try {
            if ((await send(draft)).outcome === "sent") {
                await deleteDraft(draft.id);
            }
            else {
                left += 1;
            }
        }
        catch {
            left += 1;
        }
    }
    if (left === 0) {
        window.location.assign("/today?sent=1");
        return;
    }
    text.textContent = `${left} not sent yet and kept on this phone. Try again with signal, or open the visit to see why.`;
    button.disabled = false;
}
/* Signing out deletes what the app keeps on the phone: saved pages, drafts and the service
   worker. The server's Clear-Site-Data header does the same; this covers browsers that do not
   act on it fully. An unsent note is never deleted without a second, explicit tap. */
function initSignOut(form) {
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        void signOut(form);
    });
}
async function signOut(form) {
    const unsent = (await allDrafts().catch(() => [])).length;
    if (unsent > 0 && form.dataset.confirmed !== "yes") {
        form.dataset.confirmed = "yes";
        const warning = document.createElement("p");
        warning.className = "alert";
        warning.setAttribute("role", "alert");
        warning.textContent =
            unsent === 1
                ? "1 note on this phone has not been sent. Send it first, or sign out to delete it."
                : `${unsent} notes on this phone have not been sent. Send them first, or sign out to delete them.`;
        form.prepend(warning);
        const button = form.querySelector("button");
        if (button)
            button.textContent = "Sign out and delete";
        return;
    }
    await clearPhone();
    form.submit();
}
async function clearPhone() {
    try {
        for (const name of await caches.keys()) {
            await caches.delete(name);
        }
        await new Promise((resolve) => {
            const request = indexedDB.deleteDatabase(DATABASE);
            request.onsuccess = () => resolve();
            request.onerror = () => resolve();
            request.onblocked = () => resolve();
        });
        for (const registration of await navigator.serviceWorker.getRegistrations()) {
            await registration.unregister();
        }
    }
    catch {
        // The server's Clear-Site-Data header still clears what this could not.
    }
}
/* Say when there is no signal, and how old the page is. The server is asked directly:
   navigator.onLine can be true with no usable connection (MDN), so it is not trusted. */
async function showConnection() {
    const line = document.querySelector("[data-offline]");
    const rendered = document.querySelector("main")?.dataset.rendered;
    if (!line) {
        return;
    }
    const online = await connected();
    line.hidden = online;
    if (!online) {
        const saved = rendered ? `, saved ${ago(Date.parse(rendered))}` : "";
        line.textContent = `No signal. You can keep working: this page is the copy on this phone${saved}.`;
    }
    // The waiting panel's Send button listens for this.
    document.dispatchEvent(new CustomEvent("connection", { detail: online }));
}
async function connected() {
    try {
        const answer = await fetch("/healthz", {
            cache: "no-store",
            signal: AbortSignal.timeout(5000),
        });
        // The check ends only when the whole answer is in: a body left unread keeps the request
        // open until the time limit aborts it.
        await answer.text();
        return answer.ok;
    }
    catch {
        return false;
    }
}
async function registerWorker() {
    if (!("serviceWorker" in navigator)) {
        return;
    }
    await navigator.serviceWorker.register(scripts ? scripts.createScriptURL(WORKER) : WORKER, {
        scope: "/",
    });
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
        // The first page of the day: the worker takes over from the next load, so wait for it.
        await new Promise((resolve) => {
            navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), { once: true });
        });
    }
    saveDay();
}
/* Ask the worker to save what a carer needs without signal: the start page, this sheet, and
   the note form of every visit the sheet links (those begun today, to write or to add to).
   The sheet opens again after each Send, so a form whose title changed is saved afresh. */
function saveDay() {
    const links = document.querySelectorAll('main a[href^="/visits/"]');
    const forms = new Set(Array.from(links, (link) => link.getAttribute("href") ?? ""));
    const pages = ["/today", window.location.pathname + window.location.search, ...forms];
    navigator.serviceWorker.controller?.postMessage({ save: pages });
}
/* The route on OpenStreetMap's map, shown only while there is signal; without it the server's
   sketch stays, since OSM's tiles may not be used offline. One finger scrolls the page on a
   phone (the map never traps it); pinch, the buttons or the keyboard zoom. */
function initMap(figure) {
    const holder = figure.querySelector(".live-map");
    const canvas = figure.querySelector(".live-map-canvas");
    const data = JSON.parse(figure.querySelector("#route-data")?.textContent ?? "null");
    if (!holder || !canvas || !data) {
        return;
    }
    let building;
    document.addEventListener("connection", (event) => {
        const online = event.detail;
        if (online && !building) {
            building = loadLeaflet().then((leaflet) => {
                if (!leaflet)
                    return undefined;
                holder.hidden = false; // Leaflet measures its box, so it must show before drawing
                return drawMap(leaflet, figure, canvas, data);
            });
        }
        void building?.then((map) => {
            const live = online && map !== undefined;
            figure.classList.toggle("is-live", live);
            holder.hidden = !live;
            if (live)
                map.invalidateSize();
        });
    });
}
function loadLeaflet() {
    return new Promise((resolve) => {
        const script = document.createElement("script");
        script.src = scripts ? scripts.createScriptURL(LEAFLET) : LEAFLET;
        script.onload = () => resolve(globalThis.L);
        script.onerror = () => resolve(undefined);
        document.head.append(script);
    });
}
function drawMap(leaflet, figure, canvas, data) {
    const touch = window.matchMedia("(pointer: coarse)").matches;
    const map = leaflet.map(canvas, {
        zoomControl: false, // Leaflet's own controls write HTML; ours are in the page
        attributionControl: false, // the credit is in the page, on the map, always visible
        dragging: !touch,
        scrollWheelZoom: false,
    });
    leaflet
        .tileLayer(TILES, { maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin" })
        .addTo(map);
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--link").trim();
    leaflet.polyline(data.route, { color: ink, weight: 4, interactive: false }).addTo(map);
    const next = figure.dataset.next ?? "";
    for (const stop of data.stops) {
        const label = document.createElement("span");
        label.className = stop.label.split(", ").includes(next) ? "map-stop is-next" : "map-stop";
        label.textContent = stop.label;
        pin(leaflet, map, [stop.lat, stop.lon], label);
    }
    const home = document.createElement("span");
    home.className = "map-home";
    pin(leaflet, map, data.home, home);
    const whole = () => map.fitBounds(data.route, { padding: [24, 24] });
    whole();
    const actions = {
        in: () => map.zoomIn(),
        out: () => map.zoomOut(),
        fit: whole,
    };
    for (const button of figure.querySelectorAll("[data-map]")) {
        button.addEventListener("click", () => actions[button.dataset.map ?? ""]?.());
    }
    return map;
}
// A marker drawn from an element, never from an HTML string (Trusted Types).
function pin(leaflet, map, point, element) {
    const icon = leaflet.divIcon({ html: element, className: "map-pin", iconSize: null });
    leaflet.marker(point, { icon, keyboard: false, interactive: false }).addTo(map);
}
function start() {
    const route = document.querySelector("figure.route");
    if (route)
        initMap(route);
    void registerWorker().catch(() => undefined);
    const compose = document.querySelector("form.compose");
    if (compose)
        void initWritePage(compose);
    const waiting = document.querySelector("[data-waiting]");
    if (waiting)
        void initTodayPage(waiting);
    const signOutForm = document.querySelector('form[action="/signout"]');
    if (signOutForm)
        initSignOut(signOutForm);
    void showConnection();
    window.addEventListener("online", () => void showConnection());
    window.addEventListener("offline", () => void showConnection());
}
start();
