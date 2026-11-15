// TP-07: the read-only weekly view in a real browser (CI job "frontend").
const { chromium } = require("playwright");
const path = require("node:path");
const assert = require("node:assert/strict");
const { dialogConfig } = require("./support/dialog-texts.cjs");

const DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"];

// The same data the server builds (schedule_week.week_days), for one day.
const day = (key, segments, extra = {}) => ({
  key, name: key.toUpperCase(), empty: false, changed: false, segments, ...extra,
});
const segment = (start, end, level, label, extra = {}) => ({
  start, end, level, label, point: false, carried: false, known: true, ...extra,
});

const WEEK = [
  day("mo", [
    segment(0, 390, "eco", "Absenken", { carried: true }),
    segment(390, 1320, "comfort2", "Heizen", { point: true }),
    segment(1320, 1440, "eco", "Absenken", { point: true }),
  ]),
  day("tu", [segment(0, 1440, null, null)], { empty: true }),
  day("we", [
    segment(0, 360, "eco", "Absenken", { carried: true }),
    segment(360, 375, "turbo", "turbo", { point: true, known: false }),
    segment(375, 1440, "x1", "<script>window.hacked=1</script>", { point: true }),
  ]),
  ...DAYS.slice(3).map((key) => day(key, [segment(0, 1440, "eco", "Absenken", { carried: true })])),
];

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  for (const scheme of ["light", "dark"]) {
    const page = await browser.newPage({
      viewport: { width: 380, height: 900 },
      colorScheme: scheme,
      reducedMotion: "reduce",
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("dialog", (dialog) => { errors.push("dialog"); dialog.dismiss(); });
    await page.setContent("<button id='before'>before</button><ha-selector-bosch-buderus-schedule-week></ha-selector-bosch-buderus-schedule-week><button id='after'>after</button>");
    await page.addScriptTag({
      path: path.join(__dirname, "../../custom_components/bosch_buderus_heating/www/schedule-week.js"),
    });
    await page.evaluate(({ week, texts }) => {
      const view = document.querySelector("ha-selector-bosch-buderus-schedule-week");
      view.hass = { language: "de" };
      view.selector = { "bosch-buderus-schedule-week": { days: week, title: "", ...texts } };
    }, { week: WEEK, texts: dialogConfig("schedule") });
    const result = await page.evaluate(() => {
      const root = document.querySelector("ha-selector-bosch-buderus-schedule-week").shadowRoot;
      const rows = [...root.querySelectorAll("tbody tr")];
      return {
        rows: rows.length,
        rowHeaders: [...root.querySelectorAll('th[scope="row"]')].map((th) => th.textContent),
        colHeaders: root.querySelectorAll('th[scope="col"]').length,
        labels: [...root.querySelectorAll(".seg")].map((seg) => seg.getAttribute("aria-label")),
        texts: [...root.querySelectorAll(".seg")].map((seg) => seg.textContent),
        scripts: root.querySelectorAll("script").length + document.querySelectorAll("script:not([src])").length,
        hacked: Boolean(window.hacked),
        unknown: root.querySelector(".seg.unknown")?.textContent,
        empty: rows[1].textContent,
      };
    });
    assert.equal(result.rows, 7);
    assert.deepEqual(result.rowHeaders, DAYS.map((key) => key.toUpperCase()));
    assert.equal(result.colHeaders, 2);
    assert.ok(result.labels[0].includes("00:00") && result.labels[0].includes("06:30"));
    assert.ok(result.labels[0].includes("vom Vortag"));
    // Device texts appear as text, nothing runs.
    assert.ok(result.texts.includes("<script>window.hacked=1</script>"));
    assert.equal(result.hacked, false);
    assert.equal(result.unknown, "turbo");
    assert.ok(result.empty.includes("keine Schaltpunkte"));
    assert.ok(!result.empty.includes("Aus"));
    // Tab reaches the view once, arrows move inside, Tab leaves it again.
    await page.focus("#before");
    await page.keyboard.press("Tab");
    const first = await page.evaluate(() =>
      document.querySelector("ha-selector-bosch-buderus-schedule-week").shadowRoot.activeElement?.getAttribute("aria-label"));
    assert.equal(first, result.labels[0]);
    await page.keyboard.press("ArrowRight");
    const second = await page.evaluate(() =>
      document.querySelector("ha-selector-bosch-buderus-schedule-week").shadowRoot.activeElement?.getAttribute("aria-label"));
    assert.equal(second, result.labels[1]);
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.id), "after");
    // Contrast of the segment text against its colour (>= 4.5:1).
    const contrast = await page.evaluate(() => {
      const root = document.querySelector("ha-selector-bosch-buderus-schedule-week").shadowRoot;
      const parse = (value) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
      const lum = ([r, g, b]) => {
        const c = [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
      };
      return [...root.querySelectorAll(".seg:not(.none)")].map((seg) => {
        const style = getComputedStyle(seg);
        const a = lum(parse(style.color));
        const b = lum(parse(style.backgroundColor));
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      });
    });
    assert.ok(contrast.every((value) => value >= 4.5), JSON.stringify(contrast));
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log("Weekly view: seven rows, carried and empty days, text only, keyboard and contrast passed.");
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
