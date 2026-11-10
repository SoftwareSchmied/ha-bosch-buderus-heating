// Run against ha_rrc_water_heater.py's loopback Home Assistant (W3).
const { chromium } = require("playwright");
const { expect } = require("playwright/test");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const language of ["de", "en"]) {
      const page = await browser.newPage({
        locale: language === "de" ? "de-DE" : "en-US",
        viewport: { width: language === "de" ? 1000 : 390, height: 1000 },
      });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.addInitScript(
        ({ url, token, language }) => {
          localStorage.setItem(
            "hassTokens",
            JSON.stringify({
              hassUrl: new URL(url).origin,
              access_token: token,
              token_type: "Bearer",
              expires: Date.now() + 3600000,
              expires_in: 3600,
              clientId: new URL(url).origin + "/",
            }),
          );
          localStorage.setItem("selectedLanguage", JSON.stringify(language));
        },
        {
          url: process.env.WATER_HEATER_URL,
          token: process.env.WATER_HEATER_TOKEN,
          language,
        },
      );
      await page.goto(process.env.WATER_HEATER_URL + "test-hot-water/0");
      const tile = page.locator("hui-tile-card");
      await tile.getByText(/48[,.]5/).waitFor();
      const state = () =>
        tile.evaluate(
          (element, id) => element.hass.states[id],
          process.env.WATER_HEATER_ENTITY,
        );
      // Without advertised options: Off, Comfort and Auto, never Eco/Eco+.
      assert.deepEqual((await state()).attributes.operation_list, [
        "off",
        "high",
        "ownprogram",
      ]);
      await tile.locator("ha-tile-container").click();
      const dialog = page.locator("ha-more-info-dialog");
      await dialog.waitFor({ state: "attached" });
      await page
        .locator("more-info-water_heater")
        .waitFor({ state: "attached" });
      await page
        .locator("more-info-water_heater")
        .evaluate((element) => element.updateComplete);
      const mode = dialog.getByRole("button", {
        name: language === "de" ? /Modus/ : /Mode/,
      });
      await mode.click();
      for (const label of language === "de"
        ? ["Aus", "Komfort", "Auto"]
        : ["Off", "Comfort", "Auto"]) {
        await page.getByText(label, { exact: true }).last().waitFor();
      }
      for (const absent of ["Eco", "Eco+"]) {
        assert.equal(await page.getByText(absent, { exact: true }).count(), 0);
      }
      // German: "Off" (written as "Off", read back as "off"); English: Auto.
      await page
        .getByText(language === "de" ? "Aus" : "Auto", { exact: true })
        .last()
        .click();
      await expect
        .poll(async () => (await state()).state)
        .toBe(language === "de" ? "off" : "ownprogram");
      await page.keyboard.press("Escape");
      await mode.waitFor({ state: "hidden" });
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
