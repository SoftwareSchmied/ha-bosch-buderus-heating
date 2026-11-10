// The controls form restores focus once after HA replaces it, never on
// later state updates.
const { chromium } = require("playwright");
const path = require("node:path");
const { dialogConfig } = require("./support/dialog-texts.cjs");
const assert = require("node:assert/strict");
(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  const page = await browser.newPage({ viewport: { width: 600, height: 500 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setContent("<dialog-data-entry-flow></dialog-data-entry-flow>");
  await page.addScriptTag({
    path: path.join(
      __dirname,
      "../../custom_components/bosch_buderus_heating/www/controls-dialog.js",
    ),
  });
  // The texts the server sends with every dialog step.
  await page.evaluate((dialog) => {
    window.dialog = dialog;
  }, dialogConfig("controls"));
  // The structure HA uses: the form inside the open dialog's shadow root.
  const mount = (open, disabled = false) =>
    page.evaluate(({ open, disabled }) => {
      const host = document.querySelector("dialog-data-entry-flow");
      const root = host.shadowRoot || host.attachShadow({ mode: "open" });
      root.innerHTML =
        '<ha-dialog><div class="title" style="padding:8px">Anlage bedienen</div><step-flow-form></step-flow-form></ha-dialog>';
      root.querySelector("ha-dialog").open = open;
      const flow = root.querySelector("step-flow-form");
      flow.domain = "bosch_buderus_heating";
      flow.step = { step_id: "operate", flow_id: "flow" };
      const element = document.createElement(
        "ha-selector-bosch-buderus-controls",
      );
      const rows = [];
      for (let index = 0; index < 25; index++)
        rows.push({
          key: "/heatingCircuits/hc1/value" + index,
          group: "heatingCircuits/hc1",
          kind: "number",
          label: { de: "Wert " + index, en: "Value " + index },
          value: 20,
          minimum: 5,
          maximum: 30,
          unit: "°C",
          available: true,
        });
      element.hass = { language: "de" };
      element.selector = { "bosch-buderus-controls": { ...window.dialog, rows } };
      element.value = { changes: {} };
      element.disabled = disabled;
      document.activeElement?.blur();
      flow.append(element);
    }, { open, disabled });
  const ui = page.locator("ha-selector-bosch-buderus-controls");
  const focused = () =>
    page.evaluate(() => {
      const element = document
        .querySelector("dialog-data-entry-flow")
        .shadowRoot.querySelector("ha-selector-bosch-buderus-controls");
      return {
        page: document.activeElement.localName,
        inner: element.shadowRoot.activeElement?.id || null,
      };
    });

  // A newly shown form takes the focus that HA left on the page body.
  await mount(true);
  await page.waitForTimeout(50);
  assert.deepEqual(await focused(), {
    page: "dialog-data-entry-flow",
    inner: "tab-0",
  });

  // Later, a click on empty space leaves focus on the body. State updates
  // set every property again and must neither move focus nor scroll.
  await ui.getByLabel("Wert 24", { exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const scrolled = await page.evaluate(() => window.scrollY);
  assert.ok(scrolled > 0);
  await page.mouse.click(590, 5);
  assert.equal((await focused()).page, "body");
  await ui.evaluate((element) => {
    element.disabled = false;
    element.disabled = true;
    element.disabled = false;
  });
  await page.waitForTimeout(50);
  assert.equal((await focused()).page, "body");
  assert.equal(await page.evaluate(() => window.scrollY), scrolled);

  // Repeating an unchanged value does no work at all.
  const updates = await ui.evaluate((element) => {
    let count = 0;
    const original = element._setDisabled.bind(element);
    element._setDisabled = () => {
      count++;
      original();
    };
    for (let index = 0; index < 5; index++) element.disabled = false;
    element.disabled = true;
    element.disabled = true;
    return count;
  });
  assert.equal(updates, 1);
  await ui.evaluate((element) => (element.disabled = false));

  // While the dialog is still opening, the restore waits for it once.
  await mount(false);
  await page.waitForTimeout(50);
  assert.equal((await focused()).page, "body");
  await page.evaluate(() => {
    const root = document.querySelector("dialog-data-entry-flow").shadowRoot;
    root.querySelector("ha-dialog").open = true;
    const element = root.querySelector("ha-selector-bosch-buderus-controls");
    element.disabled = true;
    element.disabled = false;
  });
  await page.waitForTimeout(50);
  assert.deepEqual(await focused(), {
    page: "dialog-data-entry-flow",
    inner: "tab-0",
  });
  await page.evaluate(() => document.activeElement.blur());
  await ui.evaluate((element) => (element.disabled = false));
  await page.waitForTimeout(50);
  assert.equal((await focused()).page, "body");

  // A form that appears while HA is still busy cannot take focus yet; it
  // does so once, when it becomes usable.
  await mount(true, true);
  await page.waitForTimeout(50);
  assert.equal((await focused()).page, "body");
  await ui.evaluate((element) => (element.disabled = false));
  await page.waitForTimeout(50);
  assert.deepEqual(await focused(), {
    page: "dialog-data-entry-flow",
    inner: "tab-0",
  });

  assert.deepEqual(errors, []);
  console.log(
    "Actual controls module: focus restored once per form, never on later state updates.",
  );
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
