// Only holiday selectors are registered. No existing element is patched.
// The server sends the texts of every language that has a data file
// (i18n/<code>.json); the profile language picks one, else English.
const holidayLanguage = (language, texts, aliases) => {
  const tag = String(language || "")
    .trim()
    .toLowerCase()
    .replace("_", "-");
  const base = tag.split("-")[0];
  const known = new Map(
    Object.keys(texts || {}).map((code) => [code.toLowerCase(), code]),
  );
  for (const candidate of [tag, aliases?.[tag], base, aliases?.[base]]) {
    if (candidate && known.has(candidate)) return known.get(candidate);
  }
  return "en";
};
const holidayText = (hass, config, key) => {
  const texts = config?.texts || {};
  const code = holidayLanguage(hass?.language, texts, config?.aliases);
  return texts[code]?.[key] ?? texts.en?.[key] ?? "";
};

class BoschBuderusHolidayDateTime extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .label { display: block; margin: 0 0 8px; color: var(--primary-text-color); }
        .fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(108px, .65fr); gap: 8px; }
        input {
          box-sizing: border-box; width: 100%; min-width: 0; min-height: 56px;
          font: inherit; padding: 12px; border: 0;
          border-radius: 4px 4px 0 0; border-bottom: 1px solid var(--secondary-text-color, #666);
          color: var(--primary-text-color, #212121); background: var(--input-fill-color, #f1f1f1);
          color-scheme: var(--ha-color-scheme, light);
        }
        input:focus { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: -2px; }
        input:disabled { opacity: .5; }
      </style>
      <span class="label"></span>
      <div class="fields"><input type="date"><input type="time" step="60"></div>`;
    this._date = this.shadowRoot.querySelector('input[type="date"]');
    this._time = this.shadowRoot.querySelector('input[type="time"]');
    for (const input of [this._date, this._time]) {
      input.addEventListener("input", () => {
        this._value = this._date.value && this._time.value
          ? `${this._date.value}T${this._time.value}` : "";
        this.dispatchEvent(new CustomEvent("value-changed", {
          detail: { value: this._value }, bubbles: true, composed: true,
        }));
      });
    }
  }
  set value(value) {
    if (value === this._value) return;
    this._value = value;
    const [date = "", time = ""] = String(value || "").split("T");
    this._date.value = date;
    this._time.value = time || this._defaultTime || "";
  }
  get value() { return this._value; }
  set selector(value) {
    this._config = value?.["bosch-buderus-holiday-datetime"];
    this._defaultTime = this._config?.default_time || "";
    if (!this._value && !this._time.value) this._time.value = this._defaultTime;
    this._labels();
  }
  set hass(value) { this._hass = value; this._labels(); }
  set label(value) { this._label = value || ""; this._labels(); }
  set required(value) { this._date.required = this._time.required = Boolean(value); }
  set disabled(value) { this._date.disabled = this._time.disabled = Boolean(value); }
  _labels() {
    this.shadowRoot.querySelector(".label").textContent = this._label || "";
    this._date.setAttribute("aria-label", `${this._label || ""}: ${holidayText(this._hass, this._config, "date")}`);
    this._time.setAttribute("aria-label", `${this._label || ""}: ${holidayText(this._hass, this._config, "time")}`);
  }
  focus() { this._date.focus(); }
  reportValidity() { return this._date.reportValidity() && this._time.reportValidity(); }
}

class BoschBuderusHolidayDelete extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        button {
          display: inline-flex; align-items: center; gap: 8px;
          padding: 12px 16px; min-height: 44px;
          border: 1px solid var(--error-color, #db4437); border-radius: 8px;
          color: var(--error-color, #db4437); background: transparent;
          font: inherit; text-align: start; cursor: pointer;
        }
        button:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; }
        button:disabled { opacity: .5; cursor: default; }
        svg { flex: 0 0 20px; width: 20px; height: 20px; fill: currentColor; }
        p { color: var(--error-color, #db4437); margin: 8px 0 0; }
        p:empty { display: none; }
      </style>
      <button type="button">
        <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12M19 4h-3.5l-1-1h-5l-1 1H5v2h14V4Z"/></svg>
        <span></span>
      </button>
      <p role="alert"></p>`;
    this._button = this.shadowRoot.querySelector("button");
    this._button.addEventListener("click", () => this._openConfirmation());
    // HA submits its outer form on every Enter keydown, which would save the
    // holiday instead of deleting it. The key opens the confirmation here and
    // never reaches the form; the native activation is suppressed so it
    // cannot ask a second time.
    for (const type of ["keydown", "keyup"]) {
      this._button.addEventListener(type, (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        if (type === "keydown" && !event.repeat) this._openConfirmation();
      });
    }
  }

  set label(value) {
    this.shadowRoot.querySelector("span").textContent = value || "";
  }

  set selector(value) {
    this._config = value?.["bosch-buderus-holiday-delete"];
  }

  set disabled(value) {
    this._disabled = Boolean(value);
    this._button.disabled = this._disabled || this._busy;
  }

  focus() { this._button.focus(); }
  reportValidity() { return true; }

  async _openConfirmation() {
    if (this._busy || this._disabled) return;
    let form;
    let flow;
    // Use the native form's public data and flow handler. Shadow roots are
    // traversed only from this selector, never through unrelated dialogs.
    for (let node = this; node; node = node.parentElement || node.getRootNode()?.host) {
      if (node.localName === "ha-form") form = node;
      if (node.localName === "step-flow-form") { flow = node; break; }
    }
    const message = this.shadowRoot.querySelector("p");
    const error = holidayText(this.hass, this._config, "deleteFailed");
    if (!form || flow?.domain !== "bosch_buderus_heating" ||
        !["holiday", "legacy_holiday"].includes(flow.step?.step_id) || !flow.flowConfig?.handleFlowStep) {
      message.textContent = error;
      return;
    }
    // HA manages both disabled states itself: the request only blocks the
    // button and afterwards keeps what HA set, before or meanwhile.
    const formWasDisabled = form.disabled;
    this._busy = true;
    this._button.disabled = true;
    form.disabled = true;
    message.textContent = "";
    const fire = (name, detail) => this.dispatchEvent(new CustomEvent(name, {
      detail, bubbles: true, composed: true,
    }));
    fire("flow-step-footer-state-changed", { loading: true });
    const flowId = flow.step.flow_id;
    try {
      const step = await flow.flowConfig.handleFlowStep(flow.hass, flowId, {
        ...form.data, holiday_delete: true,
      });
      if (this.isConnected && flow.step.flow_id === flowId) fire("flow-update", { step });
    } catch (err) {
      // D53: a flow HA no longer knows (404, e.g. after a restart) needs a
      // new dialog, not another try.
      message.textContent =
        err?.status_code === 404 || err?.status === 404
          ? holidayText(this.hass, this._config, "reopen")
          : error;
    } finally {
      this._busy = false;
      this.disabled = this._disabled;
      form.disabled = formWasDisabled;
      if (this.isConnected) fire("flow-step-footer-state-changed", { loading: false });
    }
  }
}

if (!customElements.get("ha-selector-bosch-buderus-holiday-delete")) {
  customElements.define("ha-selector-bosch-buderus-holiday-delete", BoschBuderusHolidayDelete);
}
if (!customElements.get("ha-selector-bosch-buderus-holiday-datetime")) {
  customElements.define("ha-selector-bosch-buderus-holiday-datetime", BoschBuderusHolidayDateTime);
}
