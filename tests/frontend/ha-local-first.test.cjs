// Run by ha_local_first.py against an isolated Home Assistant test server.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const de = process.env.LOCAL_SETUP_LANGUAGE === "de";
  const choice = process.env.LOCAL_FIRST_CHOICE;
  const page = await browser.newPage({
    locale: de ? "de-DE" : "en-US",
    viewport: {width: Number(process.env.LOCAL_SETUP_WIDTH), height: 1000},
  });
  const errors = [];
  // The first page of a test run loads the frontend cold.
  page.setDefaultTimeout(30000);
  page.on("pageerror", error => errors.push(error.message));
  // Which options flow the frontend uses after the setup (see config_flow).
  const optionsRequests = [];
  page.on("request", request => {
    if (request.url().includes("/api/config/config_entries/options/flow")) {
      optionsRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  try {
    // The "Add integration" dialog of Home Assistant lists the integrations in
    // a lit-virtualizer. When the dialog closes to start the flow, the
    // virtualizer rejects a pending layout promise with the plain string
    // "disconnected", which the frontend's list never handles. Whether a
    // layout is still pending depends only on timing, so this one upstream
    // rejection is counted apart; every other page error still fails the test.
    await page.addInitScript(() => {
      window.__virtualizerDisconnects = 0;
      window.addEventListener("unhandledrejection", event => {
        if (event.reason === "disconnected") {
          event.preventDefault();
          window.__virtualizerDisconnects += 1;
        }
      });
    });
    await page.addInitScript(({url, token, language}) => {
      localStorage.setItem("hassTokens", JSON.stringify({
        hassUrl: new URL(url).origin, access_token: token, token_type: "Bearer",
        expires: Date.now() + 3600000, expires_in: 3600,
        clientId: new URL(url).origin + "/",
      }));
      localStorage.setItem("selectedLanguage", JSON.stringify(language));
    }, {url: process.env.LOCAL_SETUP_URL, token: process.env.LOCAL_SETUP_TOKEN,
      language: process.env.LOCAL_SETUP_LANGUAGE});
    // Add the integration as a user does: search and select it.
    await page.goto(process.env.LOCAL_SETUP_URL + "config/integrations/dashboard");
    await page.getByRole("button", {name: de ? "Integration hinzufügen" : "Add integration", exact: true}).first().click();
    // Wait until the list of integrations is shown, then search it.
    await page.getByRole("listitem").nth(5).waitFor();
    const search = page.getByRole("textbox").last();
    await search.fill("Home Comfort");
    await page.getByRole("button", {name: process.env.LOCAL_FIRST_NAME, exact: true}).first().click();
    const options = de
      ? ["Cloud-Zugang", "Cloud und lokaler Zugang (empfohlen)", "Nur lokaler Zugang"]
      : ["Cloud access", "Cloud and local access (recommended)", "Local access only"];
    // The description names the choices too: only the menu items count.
    const item = option => page.locator("ha-md-list-item, ha-list-item, mwc-list-item").filter({hasText: option}).first();
    for (const option of options) await item(option).waitFor({timeout: 30000});
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path: path.join(process.env.LOCAL_SETUP_OUTPUT, `menu-${de ? "de" : "en"}.png`)});
    await item(options[choice === "local_only" ? 2 : 0]).click();
    if (choice === "local_only") {
      // The dialog continues in the new entry's options at the local setup.
      await page.getByText(de ? "Adresse des lokalen Gateways" : "Local gateway address", {exact: true}).first().waitFor();
      await page.getByLabel(de ? "IP-Adresse oder Hostname" : "IP address or hostname", {exact: true}).waitFor();
      if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path: path.join(process.env.LOCAL_SETUP_OUTPUT, `local-first-${de ? "de" : "en"}.png`)});
      console.log(`Options flow requests: ${JSON.stringify(optionsRequests)}`);
    } else {
      // The cloud keeps its brand form.
      await page.getByText("Connect a heating account", {exact: true}).first().waitFor();
    }
    const disconnects = await page.evaluate(() => window.__virtualizerDisconnects);
    if (disconnects) console.log(`Ignored virtualizer disconnects: ${disconnects}`);
    if (errors.length) console.log(`Page errors: ${JSON.stringify(errors)}`);
    assert.deepEqual(errors, []);
    console.log(`Setup menu (${choice}) in ${de ? "German desktop" : "English mobile"}`);
  } catch (error) {
    console.log(await page.locator("home-assistant").ariaSnapshot());
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path: path.join(process.env.LOCAL_SETUP_OUTPUT, `failure-local-first-${de ? "de" : "en"}.png`)});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
