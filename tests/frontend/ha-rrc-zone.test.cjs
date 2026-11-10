// Run against ha_rrc_zone.py's loopback Home Assistant and synthetic cloud.
const { chromium } = require("playwright");
const { expect } = require("playwright/test");
const assert = require("node:assert/strict");

const url = process.env.ZONE_URL;
const manual = process.env.ZONE_MANUAL;
const schedule = process.env.ZONE_SCHEDULE;
const clear = process.env.ZONE_CLEAR;

function attribute(page, id, name) {
  return page
    .locator("hui-tile-card")
    .first()
    .evaluate(
      (element, [entity, key]) =>
        key === "state"
          ? element.hass.states[entity].state
          : element.hass.states[entity].attributes[key],
      [id, name],
    );
}

async function openMoreInfo(page, index) {
  await page.locator("hui-tile-card").nth(index).locator("ha-tile-container").click();
  const dialog = page.locator("ha-more-info-dialog");
  await dialog.waitFor({ state: "attached" });
  const climate = page.locator("more-info-climate");
  await climate.waitFor({ state: "visible" });
  await climate.evaluate((element) => element.updateComplete);
  return dialog;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      locale: "en-US",
      viewport: { width: 1000, height: 1000 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(
      ({ url, token }) => {
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
        localStorage.setItem("selectedLanguage", JSON.stringify("en"));
      },
      { url, token: process.env.ZONE_TOKEN },
    );
    await page.goto(url + "test-zones/0");
    try {
      await page.locator("hui-tile-card").nth(1).waitFor();
    } catch (error) {
      if (process.env.ZONE_SCREENSHOT) {
        await page.screenshot({ path: process.env.ZONE_SCREENSHOT });
      }
      console.log("URL", page.url(), "tiles", await page.locator("hui-tile-card").count());
      throw error;
    }

    // Zone 1 (manual): the modes heat and auto, no off; switch to auto.
    assert.deepEqual(await attribute(page, manual, "hvac_modes"), [
      "heat",
      "auto",
    ]);
    let dialog = await openMoreInfo(page, 0);
    const mode = dialog.getByRole("button", { name: /Mode/ });
    await mode.click();
    await page.getByText("Heat", { exact: true }).last().waitFor();
    assert.equal(await page.getByText("Off", { exact: true }).count(), 0);
    await page.getByText("Auto", { exact: true }).last().click();
    await expect.poll(() => attribute(page, manual, "state")).toBe("auto");
    await page.keyboard.press("Escape");
    await page.locator("more-info-climate").waitFor({ state: "detached" });

    // Zone 2: its temporary setpoint is shown as such and cleared.
    assert.equal(await attribute(page, schedule, "temporary_override"), true);
    assert.equal(await attribute(page, schedule, "temperature"), 23.5);
    const row = page.locator("hui-button-entity-row");
    await row.getByRole("button").click();
    await expect
      .poll(() => attribute(page, schedule, "temporary_override"))
      .toBe(false);

    // A new target in the mode auto: the temporary setpoint, via the dialog.
    dialog = await openMoreInfo(page, 1);
    const minus = dialog.getByRole("button", { name: /decrease/i });
    for (let step = 0; step < 8; step += 1) {
      await minus.click();
    }
    await expect
      .poll(() => attribute(page, schedule, "temperature"), { timeout: 15000 })
      .toBe(19.5);
    assert.equal(await attribute(page, schedule, "temporary_override"), true);
    assert.equal(
      await attribute(page, schedule, "override_until_next_switch_point"),
      true,
    );
    assert.equal(await attribute(page, schedule, "state"), "auto");
    await page.keyboard.press("Escape");
    assert.deepEqual(errors, []);
    await page.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
