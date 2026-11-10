// Run against ha_rrc_events.py's loopback Home Assistant and fake cloud.
const { chromium } = require("playwright");
const { expect } = require("playwright/test");
const assert = require("node:assert/strict");

const ENTITY = process.env.EVENTS_ENTITY;

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      locale: "de-DE",
      viewport: { width: 1000, height: 900 },
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
        localStorage.setItem("selectedLanguage", JSON.stringify("de"));
      },
      { url: process.env.EVENTS_URL, token: process.env.EVENTS_TOKEN },
    );
    await page.goto(process.env.EVENTS_URL + "calendar");
    // Wait until the frontend's connection holds the calendar's state.
    const ready = () =>
      page.evaluate(
        (entity) =>
          !!document.querySelector("home-assistant")?.hass?.states?.[entity],
        ENTITY,
      );
    await expect.poll(ready, { timeout: 60000 }).toBe(true);
    // The calendar under its translated name, as the frontend sees it.
    const name = await page.evaluate(
      (entity) =>
        document.querySelector("home-assistant").hass.states[entity].attributes
          .friendly_name,
      ENTITY,
    );
    assert.match(name, /Abwesenheiten und Sondertage$/);
    const call = (message) =>
      page.evaluate(
        (body) => document.querySelector("home-assistant").hass.callWS(body),
        message,
      );
    const events = () =>
      page.evaluate(
        (entity) =>
          document
            .querySelector("home-assistant")
            .hass.callApi(
              "GET",
              `calendars/${entity}?start=2027-01-01T00:00:00Z&end=2027-04-01T00:00:00Z`,
            ),
        ENTITY,
      );
    const state = await page.evaluate(
      (entity) => document.querySelector("home-assistant").hass.states[entity],
      ENTITY,
    );
    assert.equal(state.attributes.supported_features, 7);
    // Create an absence: whole days, end exclusive.
    await call({
      type: "calendar/event/create",
      entity_id: ENTITY,
      event: { summary: "Skiurlaub", dtstart: "2027-02-01", dtend: "2027-02-03" },
    });
    let listed = await events();
    const created = listed.find((item) => item.summary === "Skiurlaub");
    assert.ok(created, JSON.stringify(listed));
    assert.deepEqual([created.start, created.end], [
      { date: "2027-02-01" },
      { date: "2027-02-03" },
    ]);
    // An overlapping absence is refused with its own message.
    const overlap = await page.evaluate(
      (body) =>
        document
          .querySelector("home-assistant")
          .hass.callWS(body)
          .then(
            () => null,
            (error) => ({ code: error.code, message: error.message }),
          ),
      {
        type: "calendar/event/create",
        entity_id: ENTITY,
        event: { summary: "Doppelt", dtstart: "2027-02-02", dtend: "2027-02-04" },
      },
    );
    assert.ok(overlap, "overlap accepted");
    assert.match(overlap.message, /überschneiden|overlap/);
    // Change it by one day, then delete it.
    await call({
      type: "calendar/event/update",
      entity_id: ENTITY,
      uid: created.uid,
      event: { summary: "Skiurlaub", dtstart: "2027-02-02", dtend: "2027-02-04" },
    });
    listed = await events();
    const moved = listed.find((item) => item.summary === "Skiurlaub");
    assert.deepEqual(moved.start, { date: "2027-02-02" });
    await call({ type: "calendar/event/delete", entity_id: ENTITY, uid: moved.uid });
    listed = await events();
    assert.equal(
      listed.some((item) => item.summary === "Skiurlaub"),
      false,
    );
    // The refused command also surfaces as an unhandled rejection of its
    // error object ("Object"); nothing else may fail.
    assert.deepEqual(
      errors.filter((message) => message !== "Object"),
      [],
    );
    console.log("Frontend: room controller events created, changed and deleted.");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
