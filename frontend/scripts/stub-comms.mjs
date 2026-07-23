// Inbox/Messages have zero rows in production (the messages table is empty), so the
// conversation pane, back button and contact slide-over can't be exercised against real data.
// This stubs the API *in the browser only* — production is never touched — so those layouts
// can actually be looked at instead of assumed.
//
// Kept rather than thrown away because the data gap is not temporary: until real SMS traffic
// lands, this is the only way to see these two screens with content in them.
//
// Usage:
//   DEV_API_TARGET=https://api.owen.santiagoproperties.uk npm run dev   (terminal 1)
//   CALLMON_TOKEN=<jwt> PEEK_OUT=<dir> npm run stub-comms               (terminal 2)
import { webkit, devices } from "@playwright/test";

const TOKEN = process.env.CALLMON_TOKEN;
const BASE = "http://localhost:5173";
const OUT = process.env.PEEK_OUT || ".";

const did = (n) => ({ number_id: "n1", phone_number: n });
const thread = (id, name, num, preview, unread) => ({
  caller_id: id, contact_number: num, contact_name: name,
  company: "Dream Team Roofing", role: "Homeowner",
  last_at: "2026-07-23T18:20:00Z", last_kind: "message", last_direction: "inbound",
  last_preview: preview, message_count: 6, call_count: 2, unread_count: unread,
  open: true, responded: false,
  sticky_number: did("+19546347207"), call_from: did("+19546347207"),
  sms_from: did("+19546347207"), sms_via_fallback: false, sms_disabled_reason: null,
});

const THREADS = [
  thread("c1", "Maria Gonzalez", "+15618373514", "Can you come out Thursday to look at the roof?", 2),
  thread("c2", null, "+19544954640", "Thanks — got the estimate, reviewing it now.", 0),
  thread("c3", "Bob Ferreira", "+19412258026", "What time is the crew arriving tomorrow morning?", 1),
];

const DETAIL = {
  contact: {
    caller_id: "c1", phone_number: "+15618373514", name: "Maria Gonzalez",
    company: "Dream Team Roofing", role: "Homeowner",
    first_seen_at: "2026-06-02T14:00:00Z", total_calls: 4,
  },
  items: [
    { type: "call", id: "k1", direction: "inbound", status: "completed",
      duration_seconds: 184, at: "2026-07-22T15:02:00Z", our_number: "+19546347207" },
    { type: "message", id: "m1", direction: "inbound", status: "received",
      body: "Hi — I saw your ad and wanted to ask about a roof inspection for my place in Boca.",
      at: "2026-07-23T17:40:00Z", our_number: "+19546347207" },
    { type: "message", id: "m2", direction: "outbound", status: "delivered",
      body: "Absolutely, we can do that. Are you free Thursday morning?",
      at: "2026-07-23T17:46:00Z", our_number: "+19546347207" },
    { type: "message", id: "m3", direction: "inbound", status: "received",
      body: "Can you come out Thursday to look at the roof?",
      at: "2026-07-23T18:20:00Z", our_number: "+19546347207" },
  ],
  notes: [{ id: "nt1", body: "Referred by her neighbour on Palm Ave.", author: "admin", created_at: "2026-07-23T18:25:00Z" }],
};

const MSG_THREADS = [
  { number_id: "n1", caller_id: "c1", caller_number: "+15618373514",
    number_phone: "+19546347207", number_label: "GBP Boca Raton", campaign_name: "GBP",
    provider: "bulkvs", last_body: "Can you come out Thursday?", last_direction: "inbound",
    last_at: "2026-07-23T18:20:00Z", message_count: 6, sms_enabled: true,
    sms_disabled_reason: null },
  { number_id: "n2", caller_id: "c2", caller_number: "+19544954640",
    number_phone: "+19546347360", number_label: "CL Ads 1", campaign_name: "Craigslist",
    provider: "bulkvs", last_body: "Thanks, reviewing the estimate.", last_direction: "outbound",
    last_at: "2026-07-22T12:00:00Z", message_count: 3, sms_enabled: true,
    sms_disabled_reason: null },
];

const b = await webkit.launch();
const ctx = await b.newContext({
  ...devices["iPhone 13"], viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2, isMobile: true, hasTouch: true,
});
await ctx.addInitScript((t) => window.localStorage.setItem("callmon_token", t), TOKEN);

const json = (route, body) =>
  route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

await ctx.route("**/api/inbox/threads", (r) => json(r, THREADS));
await ctx.route("**/api/inbox/thread/*", (r) => json(r, DETAIL));
// Order matters: routes are matched in REVERSE registration order, and "thread**" also matches
// "threads". The plural must therefore be registered LAST so the list doesn't get served the
// single-conversation object (which made .map() blow up and render an empty list).
// /api/messages/thread returns a bare Msg[] (not an envelope) — returning an object here blanks
// the whole page, since the app has no error boundary and .map() on a non-array throws.
await ctx.route("**/api/messages/thread**", (r) =>
  json(r, [
    { id: "m1", direction: "inbound", body: "Hi — is the crew still coming Thursday?",
      status: "received", num_media: 0, media_urls: [], received_at: "2026-07-23T17:40:00Z" },
    { id: "m2", direction: "outbound", body: "Yes, they'll be there between 8 and 9am.",
      status: "delivered", num_media: 0, media_urls: [], received_at: "2026-07-23T17:46:00Z" },
    { id: "m3", direction: "inbound", body: "Perfect, thank you!",
      status: "received", num_media: 0, media_urls: [], received_at: "2026-07-23T18:20:00Z" },
  ]));
await ctx.route("**/api/messages/threads**", (r) => json(r, MSG_THREADS));

// Registered LAST so it is matched FIRST (Playwright checks routes in reverse order): every
// non-GET is aborted before it can leave the browser. Clicking through a stubbed thread still
// fires real writes otherwise — mark-read POSTs to /api/inbox/thread/<id>/read, which the
// stub globs above do NOT cover because * does not span a path separator.
await ctx.route("**/api/**", (route) => {
  const m = route.request().method();
  if (m === "GET" || m === "HEAD") return route.fallback();
  console.log(`  blocked ${m} ${new URL(route.request().url()).pathname}`);
  return route.abort();
});

const p = await ctx.newPage();
const shot = async (name) => {
  await p.waitForTimeout(1200);
  await p.screenshot({ path: `${OUT}/stub-${name}.png` });
  console.log("shot", name);
};

await p.goto(`${BASE}/inbox`, { waitUntil: "domcontentloaded" });
await shot("inbox-list");

// Tap a thread: the list should be replaced by the conversation, and the URL gain ?c=c1.
await p.locator(".quo-thread").first().click();
await shot("inbox-convo");
console.log("url after tap:", p.url());

// Open the contact slide-over from the header.
await p.locator(".quo-contacttoggle").click();
await shot("inbox-contact");

// Close it, then use the back control to return to the list.
await p.locator(".quo-sideclose").click();
await p.waitForTimeout(400);
await p.locator(".quo-back").click();
await shot("inbox-back");
console.log("url after back:", p.url());

// Browser back from a conversation must return to the list, not leave the Inbox.
await p.locator(".quo-thread").first().click();
await p.waitForTimeout(600);
await p.goBack();
await p.waitForTimeout(600);
console.log("url after browser-back:", p.url());
console.log("list visible after browser-back:", await p.locator(".quo-list").isVisible());

await p.goto(`${BASE}/messages`, { waitUntil: "domcontentloaded" });
await p.locator(".msglist .clickable").first().waitFor({ timeout: 15000 });
await shot("messages-list");
await p.locator(".msglist .clickable").first().click();
await shot("messages-convo");
console.log("messages url after tap:", p.url());
await p.locator(".msgback").click();
await p.waitForTimeout(500);
console.log("messages url after back:", p.url());
console.log("msglist visible after back:", await p.locator(".msglist").isVisible());

await b.close();
