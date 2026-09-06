#!/usr/bin/env node
/**
 * Capture UI screenshots with private fields CSS-blurred.
 * Run via scripts/capture-ui-shots.sh on the lab VM.
 *
 * Env: RE_UI_URL, RE_UI_PASSWORD, RE_SHOT_DIR (default /tmp/re-ui-shots)
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer");

const UI_URL = process.env.RE_UI_URL || "http://127.0.0.1:8006";
const PASSWORD = process.env.RE_UI_PASSWORD || "";
const OUT = process.env.RE_SHOT_DIR || "/tmp/re-ui-shots";

const BLUR_CSS = `
  /* Soft-blur operator/private labels; layout stays readable */
  .plan-card-name,
  .job-row-name,
  .job-row-meta,
  .plans-detail-head h3,
  #groups-detail-name,
  #locations-detail-name,
  #plans-detail-name,
  #jobs-detail-title,
  .plans-kv dd,
  .jobs-detail-kv dd,
  .meta-row,
  .plan-card-meta,
  #jobs-list-status,
  #groups-status,
  #locations-status,
  #plans-status,
  .stat-chip strong,
  input, textarea, select,
  .vm-table td,
  code {
    filter: blur(7px) !important;
  }
`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function clickId(page, id) {
  const ok = await page.evaluate((nid) => {
    const el = document.getElementById(nid);
    if (!el) return false;
    el.click();
    return true;
  }, id);
  if (!ok) throw new Error("missing #" + id);
  await sleep(600);
}

async function main() {
  if (!PASSWORD) {
    console.error("RE_UI_PASSWORD required");
    process.exit(2);
  }
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await puppeteer.launch({
    headless: "new",
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--window-size=1440,1100"],
    defaultViewport: { width: 1440, height: 1100, deviceScaleFactor: 1 },
  });
  const page = await browser.newPage();

  try {
    await page.goto(UI_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await sleep(500);
    const appHidden = await page.evaluate(
      () => document.getElementById("app-screen")?.classList.contains("hidden") ?? true
    );
    if (appHidden) {
      await page.waitForSelector("#login-password", { timeout: 8000 });
      await page.evaluate((pw) => {
        const input = document.getElementById("login-password");
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(input, pw);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        document.getElementById("btn-login")?.click();
      }, PASSWORD);
      await page.waitForFunction(() => {
        const app = document.getElementById("app-screen");
        return app && !app.classList.contains("hidden");
      }, { timeout: 15000 });
    }

    await page.addStyleTag({ content: BLUR_CSS });

    const shots = [
      ["01-restores", "nav-jobs"],
      ["02-groups", "nav-groups"],
      ["03-locations", "nav-locations"],
      ["04-plans", "nav-plans"],
      ["05-assurance", "nav-assurance"],
      ["06-help", "nav-help"],
      ["07-settings", "nav-credentials"],
    ];

    for (const [name, nav] of shots) {
      await clickId(page, nav);
      const file = path.join(OUT, name + ".png");
      await page.screenshot({ path: file, fullPage: false });
      console.log("wrote", file);
    }

    // Sidebar collapsed variant on Help
    await clickId(page, "nav-help");
    await clickId(page, "btn-sidebar-collapse");
    await sleep(300);
    const collapsed = path.join(OUT, "08-help-sidebar-collapsed.png");
    await page.screenshot({ path: collapsed, fullPage: false });
    console.log("wrote", collapsed);
    await clickId(page, "btn-sidebar-collapse");
  } finally {
    await browser.close().catch(() => {});
  }
  console.log("SHOTS_DIR", OUT);
}

main().catch((e) => {
  console.error("FATAL", e.message || e);
  process.exit(2);
});
