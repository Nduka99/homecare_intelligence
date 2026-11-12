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
const NO_SIGNAL =
  "No signal: the note is kept on this phone. Send it from Your visits when you have signal.";

interface Draft {
  id: string; // carer and visit: one draft per visit
  carer: string;
  visit: string;
  amends: string; // the note an addition adds to, or "" for a first note
  key: string; // the idempotency key the server checks
  body: string;
  saved: number; // when it was last saved, in milliseconds since 1970
  waiting: boolean; // Send was tapped but the note has not reached the server
}

// Trusted Types are Baseline since February 2026 but not yet in TypeScript's DOM types.
interface PolicyRules {
  createScriptURL?(url: string): string;
  createHTML?(html: string): string | null;
}
interface ScriptUrlPolicy {
  createScriptURL(url: string): string;
}
interface TrustedTypesFactory {
  createPolicy(name: string, rules: PolicyRules): ScriptUrlPolicy;
}

/* The page's two Trusted Types policies, made once. "carer-app" allows exactly two scripts: the
   service worker and Leaflet. "default" lets exactly one string be written as HTML, Leaflet's
   load-time test "<svg/>"; for anything else it returns nothing, so the browser blocks the
   write and reports the violation, as it does with no default policy. */
const trusted = (globalThis as { trustedTypes?: TrustedTypesFactory }).trustedTypes;
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

// The few Leaflet calls the map makes, typed by hand (Leaflet 1.9.4 ships no types).
type Point = [number, number];
interface LeafletMap {
  fitBounds(points: Point[], options: { padding: Point }): LeafletMap;
  zoomIn(): LeafletMap;
  zoomOut(): LeafletMap;
  invalidateSize(): LeafletMap;
}
interface LeafletLayer {
  addTo(map: LeafletMap): LeafletLayer;
}
interface Leaflet {
  map(element: HTMLElement, options: Record<string, unknown>): LeafletMap;
  tileLayer(url: string, options: Record<string, unknown>): LeafletLayer;
  polyline(points: Point[], options: Record<string, unknown>): LeafletLayer;
  marker(point: Point, options: Record<string, unknown>): LeafletLayer;
  divIcon(options: Record<string, unknown>): unknown;
}
interface RouteData {
  route: Point[];
  home: Point;
  stops: { lat: number; lon: number; label: string }[];
}

function openDrafts(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  work: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const database = await openDrafts();
  try {
    return await new Promise<T>((resolve, reject) => {
      const request = work(database.transaction(STORE, mode).objectStore(STORE));
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  } finally {
    database.close();
  }
}

function saveDraft(draft: Draft): Promise<IDBValidKey> {
  return withStore("readwrite", (store) => store.put(draft));
}

function readDraft(id: string): Promise<Draft | undefined> {
  return withStore<Draft | undefined>("readonly", (store) => store.get(id));
}

function deleteDraft(id: string): Promise<undefined> {
  return withStore("readwrite", (store) => store.delete(id));
}

function allDrafts(): Promise<Draft[]> {
  return withStore<Draft[]>("readonly", (store) => store.getAll());
}

/* How long ago, in words. A clock time would clash with the demo's own clock, and "12 minutes
   ago" answers the carer's real question: how old is this copy? */
function ago(milliseconds: number): string {
  const minutes = Math.floor((Date.now() - milliseconds) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes === 1) return "1 minute ago";
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
}

function csrfToken(): string {
  return document.querySelector<HTMLInputElement>('input[name="csrf"]')?.value ?? "";
}

interface Sent {
  outcome: "sent" | "signed out" | "refused";
  url: string; // where the server sent the browser
}

/* Post a draft as the plain form would. It counts as sent only when the server answers with
   the "Your visits" page; a signed-out carer is sent to the sign-in page instead. */
async function send(draft: Draft): Promise<Sent> {
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

function field(form: HTMLFormElement, name: string): HTMLInputElement | HTMLTextAreaElement {
  const found = form.elements.namedItem(name);
  if (!(found instanceof HTMLInputElement || found instanceof HTMLTextAreaElement)) {
    throw new Error(`the note form has no ${name} field`);
  }
  return found;
}

async function initWritePage(form: HTMLFormElement): Promise<void> {
  const carer = form.dataset.carer ?? "";
  const visit = form.dataset.visit ?? "";
  const id = `${carer}/${visit}`;
  const body = field(form, "body");
  const key = field(form, "key");
  const amends = field(form, "amends");
  const state = form.querySelector<HTMLElement>(".draft-state");
  const say = (text: string): void => {
    if (state) state.textContent = text;
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
  const draft = (waiting: boolean): Draft => ({
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

async function submit(form: HTMLFormElement, draft: Draft, say: (text: string) => void) {
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
  } catch {
    say(NO_SIGNAL);
  }
}

async function initTodayPage(panel: HTMLElement): Promise<void> {
  const carer = panel.dataset.carer ?? "";
  const waiting = (await allDrafts()).filter((draft) => draft.carer === carer && draft.waiting);
  if (waiting.length === 0) {
    return;
  }
  // Name each waiting visit as the sheet does, and mark its row, so nobody writes it twice.
  const names = [];
  for (const draft of waiting) {
    const row = document.querySelector<HTMLElement>(`li[data-visit="${CSS.escape(draft.visit)}"]`);
    const client = row?.querySelector(".what strong")?.textContent ?? "a visit";
    names.push(`visit ${row?.querySelector(".sec")?.textContent ?? "?"} (${client})`);
    const mark = document.createElement("p");
    mark.className = "waiting-mark";
    mark.textContent = "Note waiting on this phone to be sent.";
    row?.querySelector(".note-state")?.after(mark);
    // The note is written, so no link offers to write it: the Send button above is now the one
    // thing to do. Every link to its form, in "To write up" and in the visit's row, becomes a
    // plain link that opens the note to change it before sending.
    const links = document.querySelectorAll<HTMLAnchorElement>(
      `main a[href="/visits/${CSS.escape(draft.visit)}/note"]`,
    );
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
  const ready = (online: boolean): void => {
    button.disabled = !online;
    button.textContent = online ? "Send now" : "Send when there is signal";
    reason.textContent = online ? "" : "No signal yet: the notes stay safe on this phone.";
  };
  ready(await connected());
  // Signal often returns with no "online" event (a phone on weak data), so ask again.
  window.setInterval(() => void showConnection(), RECHECK);
  document.addEventListener("connection", (event) => {
    ready((event as CustomEvent<boolean>).detail);
  });
  button.addEventListener("click", () => {
    void sendWaiting(waiting, text, button);
  });
  panel.append(text, button, reason);
  panel.hidden = false;
}

async function sendWaiting(waiting: Draft[], text: HTMLElement, button: HTMLButtonElement) {
  button.disabled = true;
  let left = 0;
  for (const draft of waiting) {
    try {
      if ((await send(draft)).outcome === "sent") {
        await deleteDraft(draft.id);
      } else {
        left += 1;
      }
    } catch {
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
function initSignOut(form: HTMLFormElement): void {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void signOut(form);
  });
}

async function signOut(form: HTMLFormElement): Promise<void> {
  const unsent = (await allDrafts().catch(() => [] as Draft[])).length;
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
    if (button) button.textContent = "Sign out and delete";
    return;
  }
  await clearPhone();
  form.submit();
}

async function clearPhone(): Promise<void> {
  try {
    for (const name of await caches.keys()) {
      await caches.delete(name);
    }
    await new Promise<void>((resolve) => {
      const request = indexedDB.deleteDatabase(DATABASE);
      request.onsuccess = () => resolve();
      request.onerror = () => resolve();
      request.onblocked = () => resolve();
    });
    for (const registration of await navigator.serviceWorker.getRegistrations()) {
      await registration.unregister();
    }
  } catch {
    // The server's Clear-Site-Data header still clears what this could not.
  }
}

/* Say when there is no signal, and how old the page is. The server is asked directly:
   navigator.onLine can be true with no usable connection (MDN), so it is not trusted. */
async function showConnection(): Promise<void> {
  const line = document.querySelector<HTMLElement>("[data-offline]");
  const rendered = document.querySelector<HTMLElement>("main")?.dataset.rendered;
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

async function connected(): Promise<boolean> {
  try {
    const answer = await fetch("/healthz", {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    // The check ends only when the whole answer is in: a body left unread keeps the request
    // open until the time limit aborts it.
    await answer.text();
    return answer.ok;
  } catch {
    return false;
  }
}

async function registerWorker(): Promise<void> {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  await navigator.serviceWorker.register(scripts ? scripts.createScriptURL(WORKER) : WORKER, {
    scope: "/",
  });
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) {
    // The first page of the day: the worker takes over from the next load, so wait for it.
    await new Promise<void>((resolve) => {
      navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), { once: true });
    });
  }
  saveDay();
}

/* Ask the worker to save what a carer needs without signal: the start page, this sheet, and
   the note form of every visit the sheet links (those begun today, to write or to add to).
   The sheet opens again after each Send, so a form whose title changed is saved afresh. */
function saveDay(): void {
  const links = document.querySelectorAll<HTMLAnchorElement>('main a[href^="/visits/"]');
  const forms = new Set(Array.from(links, (link) => link.getAttribute("href") ?? ""));
  const pages = ["/today", window.location.pathname + window.location.search, ...forms];
  navigator.serviceWorker.controller?.postMessage({ save: pages });
}

/* The route on OpenStreetMap's map, shown only while there is signal; without it the server's
   sketch stays, since OSM's tiles may not be used offline. One finger scrolls the page on a
   phone (the map never traps it); pinch, the buttons or the keyboard zoom. */
function initMap(figure: HTMLElement): void {
  const holder = figure.querySelector<HTMLElement>(".live-map");
  const canvas = figure.querySelector<HTMLElement>(".live-map-canvas");
  const data = JSON.parse(figure.querySelector("#route-data")?.textContent ?? "null") as RouteData;
  if (!holder || !canvas || !data) {
    return;
  }
  let building: Promise<LeafletMap | undefined> | undefined;
  document.addEventListener("connection", (event) => {
    const online = (event as CustomEvent<boolean>).detail;
    if (online && !building) {
      building = loadLeaflet().then((leaflet) => {
        if (!leaflet) return undefined;
        holder.hidden = false; // Leaflet measures its box, so it must show before drawing
        return drawMap(leaflet, figure, canvas, data);
      });
    }
    void building?.then((map) => {
      const live = online && map !== undefined;
      figure.classList.toggle("is-live", live);
      holder.hidden = !live;
      if (live) map.invalidateSize();
    });
  });
}

function loadLeaflet(): Promise<Leaflet | undefined> {
  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = scripts ? scripts.createScriptURL(LEAFLET) : LEAFLET;
    script.onload = () => resolve((globalThis as { L?: Leaflet }).L);
    script.onerror = () => resolve(undefined);
    document.head.append(script);
  });
}

function drawMap(leaflet: Leaflet, figure: HTMLElement, canvas: HTMLElement, data: RouteData) {
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
  const actions: Record<string, () => void> = {
    in: () => map.zoomIn(),
    out: () => map.zoomOut(),
    fit: whole,
  };
  for (const button of figure.querySelectorAll<HTMLButtonElement>("[data-map]")) {
    button.addEventListener("click", () => actions[button.dataset.map ?? ""]?.());
  }
  return map;
}

// A marker drawn from an element, never from an HTML string (Trusted Types).
function pin(leaflet: Leaflet, map: LeafletMap, point: Point, element: HTMLElement): void {
  const icon = leaflet.divIcon({ html: element, className: "map-pin", iconSize: null });
  leaflet.marker(point, { icon, keyboard: false, interactive: false }).addTo(map);
}

function start(): void {
  const route = document.querySelector<HTMLElement>("figure.route");
  if (route) initMap(route);
  void registerWorker().catch(() => undefined);
  const compose = document.querySelector<HTMLFormElement>("form.compose");
  if (compose) void initWritePage(compose);
  const waiting = document.querySelector<HTMLElement>("[data-waiting]");
  if (waiting) void initTodayPage(waiting);
  const signOutForm = document.querySelector<HTMLFormElement>('form[action="/signout"]');
  if (signOutForm) initSignOut(signOutForm);
  void showConnection();
  window.addEventListener("online", () => void showConnection());
  window.addEventListener("offline", () => void showConnection());
}

start();
