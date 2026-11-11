// D53: status texts of the controls dialog and the holiday delete button.
const { chromium } = require("playwright");
const path = require("node:path");
const { dialogConfig } = require("./support/dialog-texts.cjs");
const assert = require("node:assert/strict");

const WWW = path.join(__dirname, "../../custom_components/bosch_buderus_heating/www");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  const page = await browser.newPage({ locale: "de-DE" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setContent(
    "<step-flow-form><ha-selector-bosch-buderus-controls></ha-selector-bosch-buderus-controls></step-flow-form>",
  );
  await page.addScriptTag({ path: path.join(WWW, "controls-dialog.js") });
  await page.evaluate((dialog) => {
    window.dialog = dialog;
  }, dialogConfig("controls"));
  const setup = (language, status, failure) =>
    page.evaluate(
      ([language, status, failure]) => {
        const selector = document.querySelector(
          "ha-selector-bosch-buderus-controls",
        );
        const rows = ["a", "b"].map((key) => ({
          key,
          group: "system",
          kind: "number",
          label: { de: "Wert " + key, en: "Value " + key },
          value: 20,
          minimum: 5,
          maximum: 30,
          step: 1,
          unit: "°C",
          available: true,
        }));
        selector.hass = { language };
        selector.selector = {
          "bosch-buderus-controls": { ...window.dialog, rows, status },
        };
        selector.value = { changes: {} };
        const form = document.querySelector("step-flow-form");
        form.domain = "bosch_buderus_heating";
        form.step = { step_id: "operate", flow_id: "example" };
        form.hass = {};
        form.flowConfig = {
          handleFlowStep: async () => {
            if (failure === "gone")
              throw { status_code: 404, body: { message: "Invalid flow specified" } };
            if (failure === "offline") throw new Error("offline");
            return { step_id: "operate" };
          },
        };
      },
      [language, status, failure],
    );
  const ui = page.locator("ha-selector-bosch-buderus-controls");
  const status = () => ui.locator(".status").textContent();

  await setup("de", "", null);
  await ui.getByLabel("Wert a", { exact: true }).fill("21");
  assert.equal(await status(), "1 ungespeicherte Änderung");
  await ui.getByLabel("Wert b", { exact: true }).fill("22");
  assert.equal(await status(), "2 ungespeicherte Änderungen");

  await setup("en", "", null);
  await ui.getByLabel("Value a", { exact: true }).fill("21");
  assert.equal(await status(), "1 unsaved change");
  await ui.getByLabel("Value b", { exact: true }).fill("22");
  assert.equal(await status(), "2 unsaved changes");

  // Languages that put the count after a label keep their one form.
  await setup("cs", "", null);
  await ui.getByLabel("Value a", { exact: true }).fill("21");
  assert.equal(await status(), "Neuložené změny: 1");

  await setup("de", "unchanged", null);
  assert.equal(await status(), "Keine Änderungen, nichts wurde geschrieben.");
  await setup("de", "saved", null);
  assert.equal(await status(), "Änderungen bestätigt.");

  // A flow HA no longer knows asks for a new dialog, not another try.
  await setup("de", "", "gone");
  await ui.getByRole("button", { name: "Werte neu laden", exact: true }).click();
  await page.waitForFunction(() =>
    document
      .querySelector("ha-selector-bosch-buderus-controls")
      .shadowRoot.querySelector(".error").textContent,
  );
  assert.equal(
    await ui.locator(".error").textContent(),
    "Bitte schließe den Dialog und öffne ihn erneut.",
  );
  await setup("de", "", "offline");
  await ui.locator(".error").evaluate((node) => (node.textContent = ""));
  await ui.getByRole("button", { name: "Werte neu laden", exact: true }).click();
  await page.waitForFunction(() =>
    document
      .querySelector("ha-selector-bosch-buderus-controls")
      .shadowRoot.querySelector(".error").textContent,
  );
  assert.equal(
    await ui.locator(".error").textContent(),
    "Die Werte konnten nicht neu geladen werden.",
  );

  // The holiday delete button: a lost flow asks for a new dialog as well.
  const holiday = await browser.newPage({ locale: "de-DE" });
  holiday.on("pageerror", (error) => errors.push(error.message));
  await holiday.setContent(
    "<step-flow-form><ha-form><ha-selector-bosch-buderus-holiday-delete></ha-selector-bosch-buderus-holiday-delete></ha-form></step-flow-form>",
  );
  await holiday.addScriptTag({ path: path.join(WWW, "holiday-dialog.js") });
  const deleteWith = async (failure) => {
    await holiday.evaluate(
      ([dialog, failure]) => {
        const button = document.querySelector(
          "ha-selector-bosch-buderus-holiday-delete",
        );
        button.hass = { language: "de" };
        button.selector = { "bosch-buderus-holiday-delete": dialog };
        button.label = "Löschen";
        const form = document.querySelector("step-flow-form");
        form.domain = "bosch_buderus_heating";
        form.step = { step_id: "holiday", flow_id: "example" };
        form.hass = {};
        document.querySelector("ha-form").data = {};
        form.flowConfig = {
          handleFlowStep: async () => {
            throw failure === "gone" ? { status_code: 404 } : new Error("offline");
          },
        };
      },
      [dialogConfig("holiday"), failure],
    );
    const button = holiday.locator("ha-selector-bosch-buderus-holiday-delete");
    await button.getByRole("button").click();
    await holiday.waitForFunction(() =>
      document
        .querySelector("ha-selector-bosch-buderus-holiday-delete")
        .shadowRoot.querySelector("p").textContent,
    );
    const text = await button.locator("p").textContent();
    await button.locator("p").evaluate((node) => (node.textContent = ""));
    return text;
  };
  assert.equal(
    await deleteWith("gone"),
    "Bitte schließe den Dialog und öffne ihn erneut.",
  );
  assert.match(await deleteWith("offline"), /Löschbestätigung/);

  assert.deepEqual(errors, []);
  await browser.close();
  console.log(
    "Controls status: singular and plural counts, unchanged, lost flow and holiday reopen passed.",
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
