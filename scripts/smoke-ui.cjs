#!/usr/bin/env node
/**
 * Headless UI smoke for restore-engine.
 *
 * Intended to run on the lab VM (or anywhere that can reach the UI), typically
 * via scripts/smoke-ui.sh + zenika/alpine-chrome:with-puppeteer
 * (CommonJS: smoke-ui.cjs).
 *
 * Does not mutate app state beyond login + UI navigation (opens create-group
 * modal and closes it with Escape; toggles sidebar collapse).
 *
 * Env:
 *   RE_UI_URL       default http://127.0.0.1:8006
 *   RE_UI_PASSWORD  required (never logged)
 */
const puppeteer = require("puppeteer");

const UI_URL = process.env.RE_UI_URL || "http://127.0.0.1:8006";
const PASSWORD = process.env.RE_UI_PASSWORD || "";

const results = [];
const pass = (name, ok, detail = "") => {
  results.push({ name, ok: !!ok, detail });
  const line = `${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`;
  console.log(line);
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!PASSWORD) {
    console.error("RE_UI_PASSWORD is required (set by smoke-ui.sh; do not hardcode).");
    process.exit(2);
  }

  const browser = await puppeteer.launch({
    headless: "new",
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    args: [
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--window-size=1440,1100",
    ],
  });

  const page = await browser.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e.message || e)));
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  const clickId = async (id) => {
    const ok = await page.evaluate((nid) => {
      const el = document.getElementById(nid);
      if (!el) return false;
      el.click();
      return true;
    }, id);
    if (!ok) throw new Error(`missing #${id}`);
    await sleep(450);
  };

  const visible = async (id) =>
    page.evaluate((vid) => {
      const el = document.getElementById(vid);
      if (!el || el.classList.contains("hidden")) return false;
      const st = getComputedStyle(el);
      return st.display !== "none" && st.visibility !== "hidden";
    }, id);

  const gotoNav = async (id, viewId, opts = {}) => {
    await clickId(id);
    const viewOk = await visible(viewId);
    const active = await page.evaluate(
      (nid) => document.getElementById(nid)?.classList.contains("active") || false,
      id
    );
    if (opts.allowRedirectTo) {
      const redirected = await visible(opts.allowRedirectTo);
      const ok = (viewOk && active) || redirected;
      pass(
        `nav-${id}`,
        ok,
        `view=${viewOk} active=${active} redirected=${redirected}`
      );
      return;
    }
    pass(`nav-${id}`, viewOk && active, `view=${viewOk} active=${active}`);
  };

  try {
    await page.goto(UI_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await sleep(600);

    const boot = await page.evaluate(() => ({
      loginHidden: document.getElementById("login-screen")?.classList.contains("hidden") ?? null,
      appHidden: document.getElementById("app-screen")?.classList.contains("hidden") ?? null,
      hasPwd: !!document.getElementById("login-password"),
    }));
    pass("boot-dom", boot.appHidden !== null, JSON.stringify(boot));

    if (boot.appHidden !== false) {
      await page.waitForSelector("#login-password", { timeout: 8000 });
      await page.evaluate((pw) => {
        const input = document.getElementById("login-password");
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(input, pw);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        document.getElementById("btn-login")?.click();
      }, PASSWORD);
      await page.waitForFunction(() => {
        const app = document.getElementById("app-screen");
        return app && !app.classList.contains("hidden");
      }, { timeout: 15000 });
    }
    pass("login", await visible("app-screen"));
    pass("sidebar", await visible("app-sidebar"));

    await gotoNav("nav-jobs", "view-jobs");
    const restores = await page.evaluate(() => {
      const chips = document.getElementById("jobs-stat-chips");
      const list = document.getElementById("jobs-list");
      const empty = document.getElementById("jobs-empty");
      const table = document.getElementById("jobs-table");
      return {
        chipCount: chips?.querySelectorAll(".stat-chip").length || 0,
        rows: list?.querySelectorAll(".job-row").length || 0,
        emptyShown: empty ? !empty.classList.contains("hidden") : null,
        detailEmptyShown: (() => {
          const el = document.getElementById("jobs-detail-empty");
          return el ? !el.classList.contains("hidden") : null;
        })(),
        tableHidden: !table || table.hidden || table.classList.contains("hidden"),
      };
    });
    pass("restores-chips", restores.chipCount > 0, JSON.stringify(restores));
    pass(
      "restores-list-or-empty",
      restores.rows > 0 || restores.emptyShown === true,
      JSON.stringify({ rows: restores.rows, emptyShown: restores.emptyShown })
    );
    pass("restores-legacy-table-hidden", restores.tableHidden);
    pass("restores-detail-pane", restores.detailEmptyShown === true || restores.rows > 0);

    await gotoNav("nav-groups", "view-groups");
    const groups = await page.evaluate(() => ({
      cards: document.querySelectorAll("#groups-list .plan-card").length,
      cardEditBtns: document.querySelectorAll(
        "#groups-list .btn-edit-group, #groups-list .btn-del-group"
      ).length,
      detailVisible: !document.getElementById("groups-detail")?.classList.contains("hidden"),
      detailEdit: !!document.getElementById("btn-detail-edit-group"),
    }));
    pass("groups-cards", groups.cards > 0, JSON.stringify(groups));
    pass("groups-quiet-cards", groups.cardEditBtns === 0);
    pass("groups-detail-edit", groups.detailEdit && groups.detailVisible);

    await gotoNav("nav-locations", "view-locations");
    const locs = await page.evaluate(() => ({
      cards: document.querySelectorAll("#locations-list .plan-card").length,
      cardEditBtns: document.querySelectorAll(
        "#locations-list .btn-edit-loc, #locations-list .btn-del-loc"
      ).length,
      detailVisible: !document.getElementById("locations-detail")?.classList.contains("hidden"),
    }));
    pass("locations-cards", locs.cards > 0, JSON.stringify(locs));
    pass("locations-quiet-cards", locs.cardEditBtns === 0);

    await gotoNav("nav-plans", "view-plans");
    const plans = await page.evaluate(() => ({
      cards: document.querySelectorAll("#plans-list .plan-card").length,
      detailVisible: !document.getElementById("plans-detail")?.classList.contains("hidden"),
    }));
    pass("plans-cards", plans.cards > 0, JSON.stringify(plans));

    await gotoNav("nav-assurance", "view-assurance");
    await gotoNav("nav-help", "view-help");
    const help = await page.evaluate(() => ({
      steps: document.querySelectorAll("#view-help .help-goto").length,
      title: document.querySelector("#view-help h2")?.textContent?.trim() || "",
    }));
    pass("help-gotos", help.steps >= 6, JSON.stringify(help));
    await page.evaluate(() =>
      document.querySelector('#view-help .help-goto[data-view="locations"]')?.click()
    );
    await sleep(500);
    pass("help-goto-locations", await visible("view-locations"));

    await gotoNav("nav-audit", "view-audit");
    await gotoNav("nav-credentials", "view-credentials");
    // Backups may redirect to Settings when credentials are not verified yet.
    await gotoNav("nav-backups", "view-backups", { allowRedirectTo: "view-credentials" });
    await gotoNav("nav-infra", "view-infra");
    await gotoNav("nav-compliance", "view-compliance");

    await clickId("nav-groups");
    await clickId("btn-create-group");
    pass(
      "group-modal-open",
      await page.evaluate(() => document.getElementById("group-edit-modal")?.classList.contains("show") || false)
    );
    await page.keyboard.press("Escape");
    await sleep(250);
    pass(
      "group-modal-escape",
      await page.evaluate(() => !document.getElementById("group-edit-modal")?.classList.contains("show"))
    );

    await clickId("btn-sidebar-collapse");
    pass(
      "sidebar-collapse",
      await page.evaluate(() => document.getElementById("app-sidebar")?.classList.contains("collapsed") || false)
    );
    await clickId("btn-sidebar-collapse");

    const filteredConsole = consoleErrors.filter(
      (e) => !/favicon|Grafana|ResizeObserver|net::ERR_/i.test(e)
    );
    // A single 404 for an optional asset is noisy but non-fatal; fail only on many.
    pass(
      "console-errors",
      filteredConsole.length <= 1,
      filteredConsole.slice(0, 5).join(" | ")
    );
    pass("page-errors", pageErrors.length === 0, pageErrors.slice(0, 5).join(" | "));
  } finally {
    await browser.close().catch(() => {});
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nSUMMARY ${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => {
  console.error("FATAL", err && err.message ? err.message : err);
  process.exit(2);
});
