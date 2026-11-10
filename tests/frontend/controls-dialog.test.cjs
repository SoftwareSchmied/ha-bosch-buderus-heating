const { chromium } = require("playwright");
const path = require("node:path");
const { dialogConfig } = require("./support/dialog-texts.cjs");
const assert = require("node:assert/strict");
(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 760, height: 1000 },
    locale: "de-DE",
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setContent(
    "<style>body{font:16px Arial;background:#f5f5f5;margin:0;padding:16px}step-flow-form{display:block;padding:24px;max-width:560px;margin:auto;background:white;border-radius:16px;--primary-color:#087eac;--primary-text-color:#222;--secondary-text-color:#666;--divider-color:#ddd;--input-fill-color:#f1f1f1;--error-color:#b00020}h1{font-size:24px;font-weight:500;margin:0 0 24px}</style><step-flow-form><h1>Anlage bedienen</h1><ha-selector-bosch-buderus-controls></ha-selector-bosch-buderus-controls></step-flow-form>",
  );
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
  await page.evaluate(() => {
    const selector = document.querySelector(
      "ha-selector-bosch-buderus-controls",
    );
    const rows = [
      {
        key: "mode",
        group: "heatingCircuits/hc1",
        kind: "select",
        label: { de: "Betriebsart", en: "Operating mode" },
        value: "manual",
        options: [
          { value: "manual", label: { de: "Manuell", en: "Manual" } },
          { value: "auto", label: { de: "Automatisch", en: "Automatic" } },
        ],
        available: true,
      },
      {
        key: "target",
        group: "heatingCircuits/hc1",
        kind: "number",
        label: { de: "Manueller Sollwert", en: "Manual setpoint" },
        value: 19,
        minimum: 5,
        maximum: 30,
        step: 0.5,
        unit: "°C",
        available: true,
      },
      {
        key: "flow",
        group: "heatingCircuits/hc1",
        kind: "number",
        label: { de: "Vorlaufgrenze", en: "Flow limit" },
        value: 38,
        minimum: 25,
        maximum: 60,
        unit: "°C",
        available: true,
        advanced: true,
      },
      {
        key: "water",
        group: "dhwCircuits/dhw1",
        kind: "switch",
        label: { de: "Extra-Warmwasser", en: "Extra hot water" },
        value: "stop",
        options: [{ value: "start" }, { value: "stop" }],
        available: true,
      },
      {
        key: "away",
        group: "system",
        kind: "switch",
        label: { de: "Abwesenheit", en: "Away" },
        value: "off",
        options: [{ value: "on" }, { value: "off" }],
        available: true,
      },
    ];
    window.rows = rows;
    selector.hass = { language: "de" };
    selector.selector = { "bosch-buderus-controls": { ...window.dialog, rows } };
    selector.value = { changes: {} };
    window.draft = {};
    window.calls = [];
    selector.addEventListener(
      "value-changed",
      (event) => (window.draft = event.detail.value),
    );
    const form = document.querySelector("step-flow-form");
    form.domain = "bosch_buderus_heating";
    window.outerSubmits = 0;
    form.addEventListener("keydown", (event) => {
      if (event.key === "Enter") window.outerSubmits++;
    });
    form.step = { step_id: "operate", flow_id: "example" };
    form.hass = {};
    form.flowConfig = {
      handleFlowStep: async (_hass, _id, data) => {
        window.calls.push(data);
        return { step_id: "operate" };
      },
    };
  });
  const ui = page.locator("ha-selector-bosch-buderus-controls");
  await ui.getByLabel("Manueller Sollwert", { exact: true }).fill("20.5");
  assert.deepEqual(await page.evaluate(() => window.draft), {
    changes: { target: 20.5 },
    active: "heatingCircuits/hc1",
  });
  await ui
    .getByRole("tab", { name: "Warmwasser 1", exact: true })
    .press("Enter");
  assert.equal(await page.evaluate(() => window.outerSubmits), 0);
  await ui.getByRole("tab", { name: "Warmwasser 1", exact: true }).click();
  await ui.getByLabel("Extra-Warmwasser", { exact: true }).check();
  // The draft names the open tab, so the server can keep it after Apply.
  assert.deepEqual(await page.evaluate(() => window.draft), {
    changes: { target: 20.5, water: "start" },
    active: "dhwCircuits/dhw1",
  });
  assert.equal(await page.evaluate(() => window.calls.length), 0);
  await ui.getByRole("tab", { name: "Heizkreis 1", exact: true }).click();
  assert.equal(
    await ui.getByLabel("Manueller Sollwert", { exact: true }).inputValue(),
    "20.5",
  );
  await ui
    .getByRole("button", { name: "Verwerfen", exact: true })
    .press("Enter");
  assert.equal(await page.evaluate(() => window.outerSubmits), 0);
  const target = ui.getByLabel("Manueller Sollwert", { exact: true });
  assert.equal(await target.inputValue(), "19");
  // Arrow keys follow the control's step; without one any value is allowed.
  assert.equal(await target.getAttribute("step"), "0.5");
  await target.press("ArrowUp");
  assert.equal(await target.inputValue(), "19.5");
  assert.equal(
    await ui.getByLabel("Vorlaufgrenze", { exact: true }).getAttribute("step"),
    "any",
  );
  assert.equal(
    await ui
      .getByRole("heading", { name: "Weitere Einstellungen" })
      .isVisible(),
    true,
  );
  assert.equal(
    await ui.getByLabel("Vorlaufgrenze", { exact: true }).isVisible(),
    true,
  );
  assert.equal(await ui.locator("details,summary").count(), 0);
  await ui.getByLabel("Vorlaufgrenze", { exact: true }).fill("999");
  assert.equal(await ui.evaluate((el) => el.reportValidity()), false);
  await ui.getByRole("button", { name: "Verwerfen", exact: true }).click();
  for (const width of [320, 390, 736]) {
    await page.setViewportSize({ width, height: 1000 });
    const size = await ui.evaluate((el) => ({
      client: el.clientWidth,
      scroll: el.scrollWidth,
    }));
    assert.ok(size.scroll <= size.client + 1, JSON.stringify(size));
  }
  await page.setViewportSize({ width: 390, height: 1000 });
  await ui
    .getByRole("button", { name: "Werte neu laden", exact: true })
    .click();
  assert.deepEqual(await page.evaluate(() => window.calls), [
    { controls: { action: "refresh", active: "heatingCircuits/hc1" } },
  ]);
  await ui.evaluate((el) => {
    el.hass = { language: "en" };
  });
  assert.equal(
    await ui.getByLabel("Manual setpoint", { exact: true }).inputValue(),
    "19",
  );
  assert.equal(
    await ui.getByRole("heading", { name: "More settings" }).isVisible(),
    true,
  );
  assert.equal(
    await ui.getByLabel("Flow limit", { exact: true }).isVisible(),
    true,
  );
  // The dialog texts follow the profile language ("no" and "nn" read
  // Norwegian Bokmål, regional tags their base language); a row without a
  // label in that language shows the English one. The expected texts come
  // from the data files.
  const controlsTexts = dialogConfig("controls").texts;
  // Spot checks that the data files hold the expected words.
  assert.equal(controlsTexts.sv.heatingCircuit, "Värmekrets");
  assert.equal(controlsTexts.nb.moreSettings, "Flere innstillinger");
  assert.equal(controlsTexts.pt.moreSettings, "Mais definições");
  // Every language with a data file (German rows carry their own label),
  // then regional tags and aliases.
  for (const [language, code] of [
    ...Object.keys(controlsTexts)
      .filter((code) => code !== "de")
      .map((code) => [code, code]),
    ["sv-SE", "sv"],
    ["no", "nb"],
    ["nb-NO", "nb"],
    ["nn", "nb"],
    ["fi", "fi"],
    ["da", "da"],
    ["fr", "fr"],
    ["fr-CA", "fr"],
    ["pt-BR", "pt"],
    ["el", "el"],
    ["el-GR", "el"],
    ["ga", "ga"],
    ["eo", "en"],
  ]) {
    const tab = `${controlsTexts[code].heatingCircuit} 1`;
    const heading = controlsTexts[code].moreSettings;
    await ui.evaluate((el, value) => {
      el.hass = { language: value };
    }, language);
    assert.equal(
      await ui.getByRole("tab", { name: tab, exact: true }).isVisible(),
      true,
      language,
    );
    assert.equal(
      await ui.getByRole("heading", { name: heading }).isVisible(),
      true,
      language,
    );
    assert.equal(
      await ui.getByLabel("Manual setpoint", { exact: true }).inputValue(),
      "19",
      language,
    );
  }
  await ui.evaluate((el) => {
    el.hass = { language: "en" };
  });
  await ui
    .getByRole("tab", { name: "Heating circuit 1", exact: true })
    .press("ArrowRight");
  assert.equal(
    await ui
      .getByRole("tab", { name: "Hot water 1", exact: true })
      .getAttribute("aria-selected"),
    "true",
  );
  await ui.evaluate((el) => {
    el.disabled = true;
  });
  assert.equal(
    await ui.getByLabel("Extra hot water", { exact: true }).isDisabled(),
    true,
  );

  // A new step from the server opens the tab of a rejected value and marks
  // the field until it is edited.
  await ui.evaluate((el) => {
    el.disabled = false;
  });
  // A tab change alone also reports the open tab.
  await ui.getByRole("tab", { name: "Heating circuit 1", exact: true }).click();
  assert.equal(
    await page.evaluate(() => window.draft.active),
    "heatingCircuits/hc1",
  );
  await ui.evaluate((el) => {
    el.selector = {
      "bosch-buderus-controls": { ...window.dialog,
        rows: window.rows,
        active: "dhwCircuits/dhw1",
        invalid: ["water"],
      },
    };
  });
  assert.equal(
    await ui
      .getByRole("tab", { name: "Hot water 1", exact: true })
      .getAttribute("aria-selected"),
    "true",
  );
  const water = ui.getByLabel("Extra hot water", { exact: true });
  assert.equal(await water.getAttribute("aria-invalid"), "true");
  const hint = ui.getByText("Please check this value.", { exact: true });
  assert.equal(await hint.isVisible(), true);
  assert.equal(
    await water.getAttribute("aria-describedby"),
    await hint.getAttribute("id"),
  );
  assert.equal(
    await ui.getByLabel("Manual setpoint", { exact: true }).getAttribute(
      "aria-invalid",
    ),
    null,
  );
  await water.check();
  assert.equal(await water.getAttribute("aria-invalid"), null);
  assert.equal(await hint.count(), 0);
  // Without a tab from the server, the open tab stays as it is.
  await ui.evaluate((el) => {
    el.selector = {
      "bosch-buderus-controls": { ...window.dialog, rows: window.rows, status: "saved" },
    };
  });
  assert.equal(
    await ui
      .getByRole("tab", { name: "Hot water 1", exact: true })
      .getAttribute("aria-selected"),
    "true",
  );
  // An unchanged appliance value off the step grid is not an input error.
  await ui.evaluate((el) => {
    el.value = { changes: {} };
    el.selector = {
      "bosch-buderus-controls": { ...window.dialog,
        rows: window.rows.map((row) =>
          row.key === "target" ? { ...row, value: 19.3 } : row,
        ),
      },
    };
  });
  assert.equal(await ui.evaluate((el) => el.reportValidity()), true);
  // The browser counts the step from the minimum. A minimum off the step
  // grid would reject values the appliance accepts: any value is entered
  // then, and the server checks it.
  const offGrid = await ui.evaluate((el) => {
    el.value = { changes: {} };
    el.selector = {
      "bosch-buderus-controls": { ...window.dialog,
        rows: window.rows.map((row) =>
          row.key === "target"
            ? { ...row, minimum: 37.5, maximum: 60, step: 1, value: 40 }
            : row,
        ),
      },
    };
    const input = el.shadowRoot.querySelector('input[data-key="target"]');
    input.value = "38";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return {
      step: input.getAttribute("step"),
      valid: input.checkValidity(),
      report: el.reportValidity(),
    };
  });
  assert.deepEqual(offGrid, { step: "any", valid: true, report: true });
  // A minimum on the grid keeps the step for arrow keys.
  const onGrid = await ui.evaluate((el) => {
    el.selector = {
      "bosch-buderus-controls": { ...window.dialog,
        rows: window.rows.map((row) =>
          row.key === "target"
            ? { ...row, minimum: 37, maximum: 60, step: 0.5, value: 40 }
            : row,
        ),
      },
    };
    return el.shadowRoot
      .querySelector('input[data-key="target"]')
      .getAttribute("step");
  });
  assert.equal(onGrid, "0.5");
  assert.deepEqual(errors, []);
  console.log(
    "Actual controls module: drafts, tabs, discard, validation, refresh, DE/EN, keyboard, disabled controls and 320/390/736px layout passed.",
  );
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
