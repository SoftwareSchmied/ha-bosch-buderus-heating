// Called by ha_dialog.py (test_component_devices_dialog) against its isolated
// Home Assistant test server: the optional component devices, on and off.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  });
  try {
    const page = await browser.newPage({
      locale: "de-DE",
      viewport: { width: 1000, height: 1100 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") console.log(message.text());
    });
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
        localStorage.setItem("selectedLanguage", JSON.stringify("de"));
      },
      { url: process.env.CONTROLS_URL, token: process.env.CONTROLS_TOKEN },
    );
    const submit = /^(Absenden|Weiter|OK)$/i;
    const entryName = "Anlagenkomponenten";
    const menuItem = (text) =>
      page
        .locator("ha-md-list-item, ha-list-item, mwc-list-item")
        .filter({ hasText: text })
        .first();
    const openStep = async () => {
      await page.goto(
        process.env.CONTROLS_URL +
          "config/integrations/integration/bosch_buderus_heating",
      );
      await page
        .getByRole("button", { name: "Konfigurieren", exact: true })
        .click();
      await menuItem(entryName).click();
      // The list names the components in the server's language; the
      // gateway's own row is not listed.
      await page.getByText("Systemregler HMI800_2_HP").first().waitFor();
      await page.getByText("Testgerät 7").first().waitFor();
      await page
        .getByText("Komponenten als Geräte anzeigen", { exact: true })
        .first()
        .waitFor();
    };
    // Closed with the dialog's close button, so no options flow stays open.
    const close = async () => {
      await page
        .getByRole("button", { name: /schließen|close/i })
        .first()
        .click();
      await menuItem(entryName).waitFor({ state: "hidden" });
    };
    // The step has one yes/no field (a checkbox in the form).
    const toggle = () => page.getByRole("checkbox").first();
    // Its drawn box lies above the input; the keyboard works as for a user.
    const flip = async (checked) => {
      await toggle().focus();
      await page.keyboard.press("Space");
      assert.equal(await toggle().isChecked(), checked);
    };

    await openStep();
    assert.equal(await toggle().isChecked(), false);
    await flip(true);
    await page.getByRole("button", { name: submit }).first().click();
    await page
      .getByText("Die Komponenten werden jetzt als Geräte", { exact: false })
      .first()
      .waitFor();
    // OK leads back to the main menu of the same dialog.
    await page.getByRole("button", { name: submit }).first().click();
    await menuItem(entryName).waitFor();
    await close();

    await openStep();
    assert.equal(await toggle().isChecked(), true);
    await flip(false);
    await page.getByRole("button", { name: submit }).first().click();
    await page
      .getByText("Komponenten-Geräte entfernt: 2", { exact: false })
      .first()
      .waitFor();
    await page.getByRole("button", { name: submit }).first().click();
    await menuItem(entryName).waitFor();
    await close();
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
