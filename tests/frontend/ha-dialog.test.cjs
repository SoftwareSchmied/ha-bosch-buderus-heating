// Called by ha_dialog.py against its isolated Home Assistant test server.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const readline = require("node:readline");

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
    page.on("websocket", (socket) => {
      const commands = new Map();
      socket.on("framesent", ({ payload }) => {
        const data = JSON.parse(payload);
        commands.set(data.id, data.type);
      });
      socket.on("framereceived", ({ payload }) => {
        const data = JSON.parse(payload);
        if (data.error)
          console.log(
            "WebSocket error:",
            commands.get(data.id),
            data.error.code,
          );
      });
    });
    await page.addInitScript(
      ({ url, token }) => {
        window.addEventListener("unhandledrejection", (event) =>
          console.error("Unhandled rejection:", JSON.stringify(event.reason)),
        );
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
    await page.goto(
      process.env.CONTROLS_URL +
        "config/integrations/integration/bosch_buderus_heating",
    );
    await page
      .getByRole("button", { name: "Konfigurieren", exact: true })
      .click();
    await page.getByText("Anlage bedienen", { exact: true }).click();
    const ui = page.locator("ha-selector-bosch-buderus-controls");
    const target = ui.getByLabel("Manueller Sollwert", { exact: true });
    const apply = page.getByRole("button", { name: "Übernehmen", exact: true });
    const reload = ui.getByRole("button", {
      name: "Werte neu laden",
      exact: true,
    });
    const saved = ui.getByText("Änderungen bestätigt.", { exact: true });

    const reopen = async () => {
      await ui.waitFor({ state: "hidden" });
      await page
        .getByRole("button", { name: "Konfigurieren", exact: true })
        .click();
      await page.getByText("Anlage bedienen", { exact: true }).click();
      await target.waitFor();
    };
    await target.waitFor();
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
    // Escape also works after entering the options submenu, without any write.
    await target.press("Escape");
    await reopen();
    // A native select consumes the first Escape while its choice list is open.
    const mode = ui.getByLabel("Betriebsart", { exact: true });
    await mode.click();
    assert.equal(await mode.evaluate((el) => el.matches(":open")), true);
    await page.keyboard.press("Escape");
    assert.equal(await ui.isVisible(), true);
    assert.equal(await mode.evaluate((el) => el.matches(":open")), false);
    await mode.press("Escape");
    await reopen();
    // Unsaved and invalid drafts close like the native header X and are lost.
    await target.fill("99");
    await apply.press("Escape");
    await reopen();
    assert.equal(await target.inputValue(), "19");

    await target.fill("20.5");
    await apply.click();
    await saved.waitFor();
    assert.equal(await target.inputValue(), "20.5");
    // Applying replaces HA's form. Escape must still work without refocusing
    // a field or button first, as it does immediately before a submission.
    await page.keyboard.press("Escape");
    await ui.waitFor({ state: "hidden", timeout: 3000 });
    await reopen();
    assert.equal(await target.inputValue(), "20.5");
    await target.fill("99");
    await apply.click();
    const invalid = page.getByText(/Eine Eingabe ist ungültig/);
    await invalid.waitFor();
    assert.equal(await target.inputValue(), "99");
    await reload.click();
    await invalid.waitFor({ state: "hidden" });
    assert.equal(await target.inputValue(), "20.5");
    await page.keyboard.press("Escape");
    await ui.waitFor({ state: "hidden", timeout: 3000 });
    await reopen();
    assert.equal(await target.inputValue(), "20.5");
    await target.fill("22");
    await apply.click();
    await saved.waitFor();
    await target.fill("22.5");
    await ui
      .getByRole("button", { name: "Verwerfen", exact: true })
      .press("Enter");
    assert.equal(await target.inputValue(), "22");

    // PUT succeeds on the simulated gateway, then the read-back times out.
    await target.fill("23");
    await apply.click();
    await page
      .getByText(/Die letzte Änderung konnte nicht bestätigt werden/)
      .waitFor();
    assert.equal(await target.isDisabled(), true);
    assert.equal(await target.inputValue(), "23");
    await reload.click();
    await target.waitFor({ state: "hidden" });

    // Ask the Python fixture to perform one normal polling cycle, then reload
    // cached values in the browser. No additional write is sent by recovery.
    const input = readline.createInterface({ input: process.stdin });
    const polled = new Promise((resolve) => input.once("line", resolve));
    console.log("POLL_SIMULATED_GATEWAY");
    assert.equal(await polled, "POLL_COMPLETE");
    input.close();
    await reload.click();
    await target.waitFor();
    assert.equal(await target.inputValue(), "23");
    assert.equal(await target.isEnabled(), true);
    await target.fill("24");
    await apply.click();
    await saved.waitFor();

    // HA recreates the form after every step. The open tab survives Apply
    // and reload, and a rejected value opens its own tab.
    const heating = ui.getByRole("tab", { name: "Heizkreis 2", exact: true });
    const water = ui.getByRole("tab", { name: "Warmwasser 1", exact: true });
    const hotWater = ui.locator(
      'input[data-key="/dhwCircuits/dhw1/temperatureLevels/high"]',
    );
    const selected = async (tab) => (await tab.getAttribute("aria-selected")) === "true";
    assert.deepEqual(
      await ui.getByRole("tab").allTextContents(),
      ["Heizkreis 2", "Warmwasser 1"],
    );
    await water.click();
    await hotWater.fill("55");
    await apply.click();
    await saved.waitFor();
    assert.equal(await selected(water), true);
    assert.equal(await hotWater.inputValue(), "55");
    await reload.click();
    await ui
      .getByText("Änderungen werden erst mit Übernehmen geschrieben.", {
        exact: true,
      })
      .waitFor();
    assert.equal(await selected(water), true);
    await heating.click();
    await target.fill("99");
    await water.click();
    await apply.click();
    await invalid.waitFor();
    assert.equal(await selected(heating), true);
    assert.equal(await target.getAttribute("aria-invalid"), "true");
    assert.equal(
      await ui.getByText("Bitte prüfe diesen Wert.", { exact: true }).isVisible(),
      true,
    );
    await reload.click();
    await invalid.waitFor({ state: "hidden" });
    assert.equal(await target.inputValue(), "24");
    assert.equal(await target.getAttribute("aria-invalid"), null);
    await page.keyboard.press("Escape");
    await ui.waitFor({ state: "hidden" });
    // The detached selector must not hijack keys in the next options menu.
    await page
      .getByRole("button", { name: "Konfigurieren", exact: true })
      .click();
    await page.getByText("Anlage bedienen", { exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await page
      .getByText("Anlage bedienen", { exact: true })
      .waitFor({ state: "hidden" });
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
