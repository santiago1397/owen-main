// Responsive screenshot harness (see docs/RESPONSIVE_SPEC.md §10).
//
// Drives the local vite dev server with Playwright's WEBKIT engine — same engine family as
// Mobile Safari, so it actually reflects dvh and safe-area behaviour that Chromium papers over.
// Shoots every route at the iPhone widths we support and writes PNGs for review.
//
// Read-only: navigates and screenshots, never clicks send/save/delete. The dev server proxies
// to the deployed API (DEV_API_TARGET) so layouts are exercised against real data lengths,
// which is where responsive bugs actually hide.
//
// Usage:
//   DEV_API_TARGET=https://api.owen.santiagoproperties.uk npm run dev      (terminal 1)
//   CALLMON_TOKEN=<jwt> npm run shoot                                      (terminal 2)

import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { webkit, devices } from "@playwright/test";

const BASE = process.env.SHOOT_BASE || "http://localhost:5173";
const TOKEN = process.env.CALLMON_TOKEN || "";
const OUT = path.resolve(process.env.SHOOT_OUT || "screenshots");
const LABEL = process.env.SHOOT_LABEL || "current";

// The iPhone widths from the spec: SE 2/3 + 13 mini, iPhone 13-16, and Pro Max / Plus.
// Plus a desktop pass, which is the regression guard for the spec's "desktop stays
// pixel-identical" contract — shoot it before and after and the PNGs should match byte for byte.
const WIDTHS = [
  { w: 375, h: 812, name: "375-se" },
  { w: 390, h: 844, name: "390-iphone14" },
  { w: 430, h: 932, name: "430-promax" },
  { w: 1440, h: 900, name: "1440-desktop", desktop: true },
];

// Static routes. Detail routes are resolved from live data below so this list can't rot.
const ROUTES = [
  { path: "/", name: "dashboard" },
  { path: "/calls", name: "calls" },
  { path: "/callers", name: "callers" },
  { path: "/emails", name: "emails" },
  { path: "/inbox", name: "inbox" },
  { path: "/numbers", name: "numbers" },
  { path: "/flows", name: "flows" },
  { path: "/messages", name: "messages" },
  { path: "/agents", name: "agents" },
  { path: "/settings", name: "settings" },
];

async function apiGet(route) {
  const res = await fetch(`${BASE}${route}`, {
    headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
  });
  if (!res.ok) throw new Error(`${route} -> ${res.status}`);
  return res.json();
}

// Resolve one real number + one real flow so the detail screens get shot with real content.
async function detailRoutes() {
  const out = [];
  try {
    const nums = await apiGet("/api/numbers?limit=1");
    const id = (Array.isArray(nums) ? nums[0] : nums?.items?.[0])?.id;
    if (id) out.push({ path: `/numbers/${id}`, name: "number-detail" });
  } catch (e) {
    console.warn(`  ! could not resolve a number id: ${e.message}`);
  }
  try {
    const flows = await apiGet("/api/flows?limit=1");
    const id = (Array.isArray(flows) ? flows[0] : flows?.items?.[0])?.id;
    if (id) out.push({ path: `/flows/${id}`, name: "flow-editor" });
  } catch (e) {
    console.warn(`  ! could not resolve a flow id: ${e.message}`);
  }
  return out;
}

async function shoot(page, route, dir) {
  await page.goto(`${BASE}${route.path}`, { waitUntil: "domcontentloaded" });
  // NOT networkidle: Inbox and Messages poll on a 5s interval, so the network never goes idle
  // and the wait would hang until timeout on every single route.
  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(dir, `${route.name}.png`), fullPage: true });
}

const main = async () => {
  if (!TOKEN) console.warn("! CALLMON_TOKEN unset — only /login will render, the rest redirect.");

  const routes = [...ROUTES, ...(TOKEN ? await detailRoutes() : [])];
  const browser = await webkit.launch();

  for (const size of WIDTHS) {
    const dir = path.join(OUT, LABEL, size.name);
    await rm(dir, { recursive: true, force: true });
    await mkdir(dir, { recursive: true });

    const ctx = await browser.newContext({
      ...(size.desktop ? {} : devices["iPhone 13"]),
      viewport: { width: size.w, height: size.h },
      deviceScaleFactor: size.desktop ? 1 : 2,
      isMobile: !size.desktop,
      hasTouch: !size.desktop,
    });

    // HARD SAFETY RAIL: this harness points at the PRODUCTION API, so nothing it does may ever
    // mutate live data. Anything that isn't a GET/HEAD is aborted in the browser before it
    // leaves the machine. Rendering a page can fire writes you don't expect — opening an Inbox
    // thread POSTs a mark-read, for one — so "I only navigate" is not sufficient on its own.
    // Scoped to /api/** deliberately: a **/* catch-all also intercepts every Vite dev module
    // request, which slows page loads enough to break the waits. Writes only ever go to the API.
    await ctx.route("**/api/**", (route) => {
      const m = route.request().method();
      if (m === "GET" || m === "HEAD") return route.fallback();
      console.log(`  blocked ${m} ${new URL(route.request().url()).pathname}`);
      return route.abort();
    });

    // Seed the JWT before any app code runs, so <Protected> doesn't bounce us to /login.
    if (TOKEN) {
      await ctx.addInitScript((t) => {
        window.localStorage.setItem("callmon_token", t);
      }, TOKEN);
    }

    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));

    console.log(`\n== ${size.name} (${size.w}px) ==`);
    for (const route of routes) {
      try {
        await shoot(page, route, dir);
        console.log(`  ok  ${route.name}`);
      } catch (e) {
        console.log(`  FAIL ${route.name}: ${e.message}`);
      }
    }

    // /login has to be shot without a token, or <Protected> redirects straight past it.
    const anon = await browser.newContext({
      ...(size.desktop ? {} : devices["iPhone 13"]),
      viewport: { width: size.w, height: size.h },
      deviceScaleFactor: size.desktop ? 1 : 2,
      isMobile: !size.desktop,
      hasTouch: !size.desktop,
    });
    const anonPage = await anon.newPage();
    try {
      await shoot(anonPage, { path: "/login", name: "login" }, dir);
      console.log("  ok  login");
    } catch (e) {
      console.log(`  FAIL login: ${e.message}`);
    }
    await anon.close();

    if (errors.length) {
      console.log(`  page errors: ${[...new Set(errors)].slice(0, 5).join(" | ")}`);
    }
    await ctx.close();
  }

  await browser.close();
  console.log(`\nWrote ${path.join(OUT, LABEL)}`);
};

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
