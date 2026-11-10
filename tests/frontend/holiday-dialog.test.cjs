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
    "<step-flow-form><ha-form><ha-selector-bosch-buderus-holiday-delete></ha-selector-bosch-buderus-holiday-delete></ha-form></step-flow-form>",
  );
  await page.addScriptTag({
    path: path.join(
      __dirname,
      "../../custom_components/bosch_buderus_heating/www/holiday-dialog.js",
    ),
  });
  // The texts the server sends with every dialog step.
  await page.evaluate((dialog) => {
    window.dialog = dialog;
  }, dialogConfig("holiday"));
  await page.evaluate(() => {
    const flow = document.querySelector("step-flow-form");
    const form = document.querySelector("ha-form");
    const selector = document.querySelector(
      "ha-selector-bosch-buderus-holiday-delete",
    );
    selector.hass = { language: "de" };
    selector.selector = { "bosch-buderus-holiday-delete": window.dialog };
    selector.label = "Urlaubszeit vollständig löschen";
    form.disabled = false;
    form.data = {
      holiday_start: "2026-12-24T08:00",
      holiday_end: "2026-12-31T08:00",
    };
    flow.domain = "bosch_buderus_heating";
    flow.step = { step_id: "holiday", flow_id: "example" };
    flow.hass = {};
    window.calls = [];
    window.updates = [];
    window.during = [];
    window.fail = false;
    // Mirrors step-flow-form of HA 2026.8 and 2026.10: every Enter keydown
    // submits the form data, which saves the holiday.
    window.outerSubmits = 0;
    flow.addEventListener("keydown", (event) => {
      if (event.key === "Enter") window.outerSubmits++;
    });
    flow.addEventListener("flow-update", (event) =>
      window.updates.push(event.detail.step),
    );
    flow.flowConfig = {
      handleFlowStep: async (_hass, _id, data) => {
        window.calls.push(data);
        window.during.push([selector._button.disabled, form.disabled]);
        if (window.disableMeanwhile) selector.disabled = true;
        await new Promise((resolve) => setTimeout(resolve, 50));
        if (window.fail) throw new Error("offline");
        return { step_id: "delete_holiday" };
      },
    };
  });
  const ui = page.locator("ha-selector-bosch-buderus-holiday-delete");
  const button = ui.getByRole("button", {
    name: "Urlaubszeit vollständig löschen",
    exact: true,
  });
  const state = () =>
    page.evaluate(() => ({
      outerSubmits: window.outerSubmits,
      calls: window.calls,
      updates: window.updates.length,
      during: window.during,
      formDisabled: document.querySelector("ha-form").disabled,
    }));
  const deletion = {
    holiday_start: "2026-12-24T08:00",
    holiday_end: "2026-12-31T08:00",
    holiday_delete: true,
  };

  // UI-1: Enter on the button opens the confirmation and never submits.
  await button.press("Enter");
  await page.waitForTimeout(150);
  let now = await state();
  assert.equal(now.outerSubmits, 0);
  assert.deepEqual(now.calls, [deletion]);
  assert.equal(now.updates, 1);
  assert.deepEqual(now.during, [[true, true]]);
  assert.equal(now.formDisabled, false);
  assert.equal(await button.isDisabled(), false);

  // Space as well, exactly once (no second native activation on keyup).
  await button.press(" ");
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.outerSubmits, 0);
  assert.deepEqual(now.calls, [deletion, deletion]);
  assert.equal(now.updates, 2);

  // Space asks on keydown already; releasing it later asks nothing more.
  await button.focus();
  await page.keyboard.down(" ");
  await page.waitForTimeout(150);
  assert.equal((await state()).calls.length, 3);
  await page.keyboard.up(" ");
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.outerSubmits, 0);
  assert.equal(now.calls.length, 3);
  assert.equal(now.updates, 3);

  // A held key asks once while the confirmation is being opened.
  await button.focus();
  await page.keyboard.down("Enter");
  await page.keyboard.down("Enter");
  await page.keyboard.up("Enter");
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.outerSubmits, 0);
  assert.equal(now.calls.length, 4);

  // The pointer still works; the form state HA set before is kept.
  await page.evaluate(() => {
    document.querySelector("ha-form").disabled = "kept";
  });
  await button.click();
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.calls.length, 5);
  assert.equal(now.formDisabled, "kept");
  await page.evaluate(() => {
    document.querySelector("ha-form").disabled = false;
  });

  // A failed request shows the message and restores the states.
  await page.evaluate(() => (window.fail = true));
  await button.press("Enter");
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.outerSubmits, 0);
  assert.equal(now.calls.length, 6);
  assert.equal(now.updates, 5);
  assert.match(
    await ui.getByRole("alert").textContent(),
    /Löschbestätigung konnte nicht geöffnet werden/,
  );
  assert.equal(now.formDisabled, false);
  assert.equal(await button.isDisabled(), false);

  // A selector that HA disables meanwhile stays disabled afterwards, and
  // a disabled one asks nothing.
  await page.evaluate(() => {
    window.fail = false;
    window.disableMeanwhile = true;
  });
  await button.press("Enter");
  await page.waitForTimeout(150);
  now = await state();
  assert.equal(now.calls.length, 7);
  assert.equal(await button.isDisabled(), true);
  await ui.evaluate((el) => el._openConfirmation());
  assert.equal((await state()).calls.length, 7);

  // Other keys keep their usual meaning for the outer form.
  await page.evaluate(() => {
    window.disableMeanwhile = false;
    document.querySelector(
      "ha-selector-bosch-buderus-holiday-delete",
    ).disabled = false;
  });
  await button.press("Tab");
  assert.equal((await state()).calls.length, 7);

  // The date and time fields name themselves in the profile language:
  // each tag with the data file whose texts it should show.
  const holidayTexts = dialogConfig("holiday").texts;
  const expected = Object.fromEntries(
    [
      ["de", "de"],
      ["en", "en"],
      ["sv", "sv"],
      ["nb-NO", "nb"],
      ["no", "nb"],
      ["nn", "nb"],
      ["fi", "fi"],
      ["da", "da"],
      ["fr", "fr"],
      ["pt-BR", "pt"],
      ["el", "el"],
      ["ga", "ga"],
      ["eo", "en"],
    ].map(([language, code]) => [
      language,
      [
        `Start: ${holidayTexts[code].date}`,
        `Start: ${holidayTexts[code].time}`,
      ],
    ]),
  );
  // Spot checks that the data files hold the expected words.
  assert.deepEqual(expected.de, ["Start: Datum", "Start: Uhrzeit"]);
  assert.deepEqual(expected.en, ["Start: Date", "Start: Time"]);
  assert.deepEqual(expected.fr, ["Start: Date", "Start: Heure"]);
  const labels = await page.evaluate((expected) => {
    const field = document.createElement(
      "ha-selector-bosch-buderus-holiday-datetime",
    );
    document.body.append(field);
    field.label = "Start";
    field.selector = {
      "bosch-buderus-holiday-datetime": { default_time: "", ...window.dialog },
    };
    const result = {};
    for (const language of Object.keys(expected)) {
      field.hass = { language };
      result[language] = [...field.shadowRoot.querySelectorAll("input")].map(
        (input) => input.getAttribute("aria-label"),
      );
    }
    field.remove();
    return result;
  }, expected);
  assert.deepEqual(labels, expected);
  assert.deepEqual(errors, []);
  console.log(
    "Actual holiday module: Enter, Space and pointer open the deletion confirmation once without submitting the form; failure message and restored states passed.",
  );
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
