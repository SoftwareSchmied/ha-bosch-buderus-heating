// Run by ha_rescan.py against an isolated Home Assistant test server.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const de = process.env.LOCAL_SETUP_LANGUAGE === "de";
  const phase = process.env.RESCAN_PHASE;
  const output = process.env.LOCAL_SETUP_OUTPUT;
  const page = await browser.newPage({
    locale: de ? "de-DE" : "en-US",
    viewport: {width: 1100, height: 900},
  });
  const errors = [];
  const consoleErrors = [];
  page.setDefaultTimeout(30000);
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  const shot = async name => {
    if (output) await page.screenshot({path: path.join(output, `${name}-${de ? "de" : "en"}.png`)});
  };
  const texts = de
    ? {
      entry: "Anlage neu durchsuchen",
      configure: "Konfigurieren",
      planned: "Die Suche ist geplant und läuft",
      running: "Eine Suche läuft bereits oder ist geplant.",
      paused: "Die Suche wurde nicht gestartet: Anfragen sind gerade pausiert",
      submit: /^(Absenden|Weiter|OK)$/i,
    }
    : {
      entry: "Search the system again",
      configure: "Configure",
      planned: "The search is planned and runs",
      running: "A search is already running or planned.",
      paused: "The search was not started: requests are paused right now",
      submit: /^(Submit|Next|OK)$/i,
    };
  const openMenu = async () => {
    await page.goto(process.env.LOCAL_SETUP_URL + "config/integrations/integration/bosch_buderus_heating");
    await page.getByRole("button", {name: texts.configure, exact: true}).click();
    const item = page.locator("ha-md-list-item, ha-list-item, mwc-list-item").filter({hasText: texts.entry}).first();
    await item.waitFor();
    return item;
  };
  const search = async (expected, name) => {
    const item = await openMenu();
    await item.click();
    // The confirmation form names the action as its title.
    await page.getByRole("button", {name: texts.submit}).first().click();
    await page.getByText(expected, {exact: false}).first().waitFor();
    await shot(name);
  };
  try {
    await page.addInitScript(({url, token, language}) => {
      localStorage.setItem("hassTokens", JSON.stringify({
        hassUrl: new URL(url).origin, access_token: token, token_type: "Bearer",
        expires: Date.now() + 3600000, expires_in: 3600,
        clientId: new URL(url).origin + "/",
      }));
      localStorage.setItem("selectedLanguage", JSON.stringify(language));
    }, {url: process.env.LOCAL_SETUP_URL, token: process.env.LOCAL_SETUP_TOKEN,
      language: process.env.LOCAL_SETUP_LANGUAGE});
    if (phase === "start") {
      await openMenu();
      await shot("rescan-menu");
      await search(texts.planned, "rescan-planned");
      await search(texts.running, "rescan-running");
    } else {
      await search(texts.paused, "rescan-paused");
    }
    const ours = consoleErrors.filter(text => /bosch_buderus_heating|home.comfort/i.test(text));
    if (consoleErrors.length) console.log(`Console errors: ${JSON.stringify(consoleErrors)}`);
    assert.deepEqual(errors, []);
    assert.deepEqual(ours, []);
    console.log(`Rescan dialog (${phase}) in ${de ? "German" : "English"}`);
  } catch (error) {
    console.log(await page.locator("home-assistant").ariaSnapshot());
    await shot(`failure-rescan-${phase}`);
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
