// Run by ha_local_setup.py against an isolated Home Assistant test server.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const de = process.env.LOCAL_SETUP_LANGUAGE === "de";
  const page = await browser.newPage({
    locale: de ? "de-DE" : "en-US",
    viewport: {width: Number(process.env.LOCAL_SETUP_WIDTH), height: 1000},
  });
  const errors = [];
  page.setDefaultTimeout(15000);
  page.on("pageerror", error => errors.push(error.message));
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
    const discoveryTitle = de ? "Lokales Gateway (192.0.2.1)" : "Local gateway (192.0.2.1)";
    await page.goto(process.env.LOCAL_SETUP_URL + "config/integrations/dashboard");
    await page.getByText(discoveryTitle, {exact: true}).waitFor();
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path:path.join(process.env.LOCAL_SETUP_OUTPUT, `discovery-${de ? "de" : "en"}.png`)});
    await page.goto(process.env.LOCAL_SETUP_URL + "config/integrations/integration/bosch_buderus_heating");
    await page.getByRole("button", {name: de ? "Konfigurieren" : "Configure", exact: true}).click();
    await page.getByText(de ? "Lokalen Zugang einrichten" : "Set up local access", {exact: true}).click();
    const next = () => page.getByRole("button", {name: /^(Submit|Next|Absenden|Weiter|OK)$/i}).click();
    const choose = async name => {
      const radio = page.getByRole("radio", {name, exact: true});
      await radio.check();
    };
    await choose("000123");
    await next();
    const address = page.getByLabel(de ? "IP-Adresse oder Hostname" : "IP address or hostname", {exact:true});
    await address.click();
    const addressInput = page.getByPlaceholder(de ? "Suchen | Benutzerdefiniertes Element hinzufügen" : "Search | Add custom item", {exact: true});
    await addressInput.fill("gateway.example.invalid");
    await page.getByText(de ? "Benutzerdefiniertes Element hinzufügen" : "Add custom item", {exact: true}).click();
    await next();
    await choose(de ? "Vorhandener Token" : "Existing token");
    await next();
    await page.getByText(de ? "Lokaler Zugriffstoken*" : "Local access token*", {exact: true}).waitFor();
    const token = page.locator('input[type="password"]');
    await token.fill("synthetic-new-local-token");
    assert.equal(await token.getAttribute("type"), "password");
    await next();
    await page.getByText(de ? "Ich bestätige dieses Gateway und sein Zertifikat" : "I confirm this gateway and its certificate", {exact:true}).first().waitFor();
    const certificate = page.getByRole("switch");
    await certificate.waitFor();
    assert.equal(await certificate.isChecked(), false);
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path:path.join(process.env.LOCAL_SETUP_OUTPUT, `certificate-${de ? "de" : "en"}.png`)});
    await page.locator("ha-switch").click();
    assert.equal(await certificate.isChecked(), true);
    await next();
    await page.getByText(de ? "Diesen lokalen Zugang speichern" : "Save this local connection", {exact:true}).first().waitFor();
    const save = page.getByRole("switch");
    await save.waitFor();
    assert.equal(await save.isChecked(), false);
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path:path.join(process.env.LOCAL_SETUP_OUTPUT, `confirmation-${de ? "de" : "en"}.png`)});
    await page.locator("ha-switch").click();
    assert.equal(await save.isChecked(), true);
    await next();
    await page.getByText(de ? "Der lokale Zugang wurde eingerichtet." : "The local access has been set up.", {exact:true}).waitFor();
    await page.goto(process.env.LOCAL_SETUP_URL + "config/integrations/dashboard");
    await page.getByRole("button", {name: de ? "Integration hinzufügen" : "Add integration", exact: true}).waitFor();
    assert.equal(await page.getByText(discoveryTitle, {exact: true}).count(), 0);
    assert.deepEqual(errors, []);
    console.log(`Local setup completed in ${de ? "German desktop" : "English mobile"}`);
  } catch (error) {
    console.log(await page.locator("home-assistant").ariaSnapshot());
    if (process.env.LOCAL_SETUP_OUTPUT) await page.screenshot({path:path.join(process.env.LOCAL_SETUP_OUTPUT, `failure-${de ? "de" : "en"}.png`)});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
