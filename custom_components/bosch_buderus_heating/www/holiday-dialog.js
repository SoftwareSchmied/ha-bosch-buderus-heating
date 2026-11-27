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

 
// Scope visibility to this holiday form, without patching HA prototypes.
class BoschBuderusHolidayAssignment extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; }
        .label { display:block; margin-bottom:8px; }
        label { display:flex; align-items:center; gap:12px; min-height:44px; cursor:pointer; }
        input { width:20px; height:20px; accent-color:var(--primary-color, #03a9f4); margin:0; }
        .helper { color:var(--secondary-text-color); font-size:.85em; }
        .error { color:var(--error-color); font-size:.85em; }
        .error:empty, .helper:empty { display:none; }
      </style>
      <span class="label" id="label"></span>
      <div class="options" role="group" aria-labelledby="label"></div>
      <div class="helper"></div><div class="error" role="alert"></div>`;
    this._options = [];
    this._value = [];
    this._onFormChange = () => this._schedule();
  }
  connectedCallback() {
    this._schedule();
  }
  disconnectedCallback() {
    this._form?.removeEventListener("value-changed", this._onFormChange);
    this._observer?.disconnect();
    cancelAnimationFrame(this._frame);
    for (const [field, display] of this._hidden || []) field.style.display = display;
    this._hidden?.clear();
    this._form = undefined;
  }
  set value(value) {
    this._value = Array.isArray(value) ? [...value] : [];
    this._updateChecks();
    this._schedule();
  }
  get value() { return this._value; }
  set selector(value) {
    const config = value?.["bosch-buderus-holiday-assignment"];
    this._config = config;
    this._options = Array.isArray(config?.options) ? config.options : [];
    const container = this.shadowRoot.querySelector(".options");
    container.replaceChildren();
    for (const option of this._options) {
      const row = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = typeof option === "string" ? option : option.value;
      const label = document.createElement("span");
      label.textContent = typeof option === "string" ? option : option.label;
      row.append(input, label);
      container.append(row);
      input.addEventListener("change", () => {
        this.value = [...container.querySelectorAll("input:checked")].map((el) => el.value);
        this.dispatchEvent(new CustomEvent("value-changed", {
          detail: { value: this._value }, bubbles: true, composed: true,
        }));
      });
    }
    this._updateChecks();
  }
  set label(value) { this.shadowRoot.querySelector(".label").textContent = value || ""; }
  set helper(value) { this.shadowRoot.querySelector(".helper").textContent = value || ""; }
  set disabled(value) { this._disabled = Boolean(value); this._updateChecks(); }
  set required(value) { this._required = Boolean(value); }
  focus() { this.shadowRoot.querySelector("input")?.focus(); }
  reportValidity() {
    const valid = this._value.length > 0;
    this.shadowRoot.querySelector(".error").textContent = valid ? "" : holidayText(this._hass, this._config, "selectCircuit");
    return valid;
  }
  set hass(value) { this._hass = value; }
  _updateChecks() {
    for (const input of this.shadowRoot.querySelectorAll("input")) {
      input.checked = this._value.includes(input.value);
      input.disabled = this._disabled || false;
    }
    if (this._value.length) this.shadowRoot.querySelector(".error").textContent = "";
  }
  _schedule() {
    if (!this.isConnected) return;
    cancelAnimationFrame(this._frame);
    this._frame = requestAnimationFrame(() => this._sync());
  }
  _sync() {
    if (!this.isConnected) return;
    let form;
    for (let node = this; node; node = node.parentElement || node.getRootNode()?.host) {
      if (node.localName === "ha-form") { form = node; break; }
    }
    if (!form?.shadowRoot) return;
    if (form !== this._form) {
      this._form?.removeEventListener("value-changed", this._onFormChange);
      this._observer?.disconnect();
      this._form = form;
      this._hidden = new Map();
      form.addEventListener("value-changed", this._onFormChange);
      this._observer = new MutationObserver(this._onFormChange);
      this._observer.observe(form.shadowRoot, { childList: true, subtree: true });
    }
    const selected = Array.isArray(this._value) ? this._value : [];
    const has = (prefix) => selected.some((item) => String(item).toLowerCase().startsWith(prefix));
    const heating = has("hc");
    const dhw = has("dhw");
    const mode = String(form.data?.holiday_heating_mode || "").toLowerCase();
    const visibility = {
      holiday_heating_mode: heating,
      holiday_fix_temperature: heating && ["fix_temp", "fix_temperature"].includes(mode),
      holiday_dhw_mode: dhw,
      holiday_thermal_disinfection: dhw,
      holiday_ventilation_mode: has("vent"),
    };
    for (const field of form.shadowRoot.querySelectorAll("ha-selector")) {
      const name = field.name || field.schema?.name;
      if (!Object.hasOwn(visibility, name)) continue;
      if (!visibility[name] && !this._hidden.has(field)) {
        this._hidden.set(field, field.style.display);
        field.style.display = "none";
      } else if (visibility[name] && this._hidden.has(field)) {
        field.style.display = this._hidden.get(field);
        this._hidden.delete(field);
      }
    }
  }
}
if (!customElements.get("ha-selector-bosch-buderus-holiday-assignment")) {
  customElements.define("ha-selector-bosch-buderus-holiday-assignment", BoschBuderusHolidayAssignment);
}


// Keep HA's two validated ISO dates, but expose a single range control.
// The end selector remains mounted for native form validation and submission.
class BoschBuderusHolidayDateRange extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; }
        button { font:inherit; color:var(--primary-text-color); cursor:pointer; }
        .open { width:100%; min-height:56px; padding:12px; text-align:start;
          background:var(--input-fill-color, var(--secondary-background-color));
          border:0; border-bottom:1px solid var(--secondary-text-color); border-radius:4px; }
        .label { display:block; margin-bottom:8px; }
        dialog { color:var(--primary-text-color); background:var(--card-background-color);
          border:1px solid var(--divider-color); border-radius:12px; padding:16px;
          width:min(340px, calc(100vw - 48px)); max-height:85vh; box-sizing:border-box; }
        dialog::backdrop { background:#0008; }
        .nav,.actions { display:flex; align-items:center; justify-content:space-between; gap:8px; }
        .nav button,.actions button { padding:8px; border:0; background:transparent; min-height:40px; }
        .days { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:2px; margin:12px 0; }
        .days button { border:0; background:transparent; min-height:36px; border-radius:6px; padding:2px; }
        .days button.inside { background:var(--primary-color); color:var(--text-primary-color,#fff); opacity:.7; }
        .days button.edge { background:var(--primary-color); color:var(--text-primary-color,#fff); }
        .weekday { text-align:center; font-size:.8em; color:var(--secondary-text-color); }
        .hint,.range,.summary { margin:8px 0; font-size:.9em; }
        .summary { color:var(--secondary-text-color); white-space:pre-line; }
        .summary:empty { display:none; }
        button:focus-visible { outline:2px solid var(--primary-color); }
        button:disabled { opacity:.45; cursor:default; }
      </style>
      <span class="label"></span><button type="button" class="open" aria-haspopup="dialog"></button><div class="summary"></div>
      <dialog><div class="nav"><button type="button" class="prev">‹</button>
      <strong class="month"></strong><button type="button" class="next">›</button></div>
      <div class="hint" aria-live="polite"></div><div class="range"></div>
      <div class="days"></div><div class="actions">
      <button type="button" class="cancel"></button><button type="button" class="apply"></button>
      </div></dialog>`;
    this._open = this.shadowRoot.querySelector(".open");
    this._dialog = this.shadowRoot.querySelector("dialog");
    this._open.addEventListener("click", () => this._show());
    this.shadowRoot.querySelector(".prev").addEventListener("click", () => this._move(-1));
    this.shadowRoot.querySelector(".next").addEventListener("click", () => this._move(1));
    this.shadowRoot.querySelector(".cancel").addEventListener("click", () => this._dialog.close());
    this.shadowRoot.querySelector(".apply").addEventListener("click", () => this._apply());
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (["Enter", " ", "Escape"].includes(event.key)) event.stopPropagation();
    });
  }
  connectedCallback() { this._schedule(); }
  disconnectedCallback() {
    cancelAnimationFrame(this._frame);
    this._form?.removeEventListener("value-changed", this._onFormChange);
    this._observer?.disconnect();
    this._form = undefined;
    if (this._hiddenHost) this._hiddenHost.style.display = this._previousDisplay;
    this._hiddenHost = undefined;
  }
  _schedule() {
    if (!this.isConnected) return;
    cancelAnimationFrame(this._frame);
    this._frame = requestAnimationFrame(() => this._sync());
  }
  _sync() {
    let form;
    let host;
    for (let node = this; node; node = node.parentElement || node.getRootNode()?.host) {
      if (node.localName === "ha-selector" && !host) host = node;
      if (node.localName === "ha-form") { form = node; break; }
    }
    if (!form?.shadowRoot) return;
    if (form !== this._form) {
      this._form = form;
      this._onFormChange = () => this._schedule();
      form.addEventListener("value-changed", this._onFormChange);
      this._observer = new MutationObserver(this._onFormChange);
      this._observer.observe(form.shadowRoot, { childList:true, subtree:true });
    }
    const pair = this._pair();
    // Only hide the redundant endpoint when both controls exist.
    if (this._config?.endpoint === "holiday_end" && pair.holiday_start && host && !this._hiddenHost) {
      this._hiddenHost = host;
      this._previousDisplay = host.style.display;
      host.style.display = "none";
    }
    this._refresh();
  }
  set selector(value) { this._config = value?.["bosch-buderus-holiday-date-range"]; this._refresh(); this._schedule(); }
  set hass(value) { this._hass = value; this._refresh(); }
  set label(value) { this._label = value || ""; this._refresh(); }
  set value(value) { this._value = value || ""; this._refresh(); this._schedule(); }
  get value() { return this._value || ""; }
  set disabled(value) { this._open.disabled = Boolean(value); }
  set required(value) { this._required = Boolean(value); }
  focus() { (this._pair().holiday_start || this)._open.focus(); }
  reportValidity() { return !this._required || Boolean(this.value); }
  _text(key) { return holidayText(this._hass, this._config, key); }
  _date(value) { return new Date(value + "T12:00:00Z"); }
  _iso(value) { return value.toISOString().slice(0, 10); }
  _format(value) {
    return value ? new Intl.DateTimeFormat(this._hass?.language || "en", {
      year:"numeric", month:"2-digit", day:"2-digit", timeZone:"UTC",
    }).format(this._date(value)) : "";
  }
  _refresh() {
    const pair = this._pair();
    const start = pair.holiday_start?.value || "";
    const end = pair.holiday_end?.value || "";
    const valid = start && end && end >= start;
    this.shadowRoot.querySelector(".label").textContent = this._text("rangeLabel");
    this._open.textContent = [this._format(start), this._format(end)].filter(Boolean).join(" – ") || this._text("selectRange");
    let summary = "";
    if (valid) {
      const days = Math.round((this._date(end) - this._date(start)) / 86400000) + 1;
      const resume = this._date(end);
      resume.setUTCDate(resume.getUTCDate() + 1);
      const duration = days === 1 ? this._text("durationOne") : this._text("durationMany");
      summary = duration.replace("{days}", String(days)) + "\n" +
        this._text("resumeHeating").replace("{date}", this._format(this._iso(resume)));
    } else if (start || end) {
      summary = this._text("invalidRange");
    }
    this.shadowRoot.querySelector(".summary").textContent = summary;
  }
  _pair() {
    let form;
    for (let node = this; node; node = node.parentElement || node.getRootNode()?.host) {
      if (node.localName === "ha-form") { form = node; break; }
    }
    const pair = {};
    for (const host of form?.shadowRoot?.querySelectorAll("ha-selector") || []) {
      const field = host.shadowRoot?.querySelector("ha-selector-bosch-buderus-holiday-date-range");
      if (field?._config?.endpoint) pair[field._config.endpoint] = field;
    }
    pair[this._config?.endpoint] = this;
    return pair;
  }
  _show() {
    this._fields = this._pair();
    this._start = this._fields.holiday_start?.value || "";
    this._end = this._fields.holiday_end?.value || "";
    this._selectEnd = false;
    const anchor = this._start || this._end;
    const date = anchor ? this._date(anchor) : new Date();
    this._month = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1, 12));
    this._render();
    this._dialog.showModal();
  }
  _move(delta) {
    this._month.setUTCMonth(this._month.getUTCMonth() + delta);
    this._render();
  }
  _render() {
    const root = this.shadowRoot;
    const language = this._hass?.language || "en";
    root.querySelector(".month").textContent = new Intl.DateTimeFormat(language, {
      month:"long", year:"numeric", timeZone:"UTC",
    }).format(this._month);
    root.querySelector(".prev").setAttribute("aria-label", this._text("previousMonth"));
    root.querySelector(".next").setAttribute("aria-label", this._text("nextMonth"));
    root.querySelector(".hint").textContent = this._selectEnd
      ? this._text("lastHolidayDay")
      : this._text("firstHolidayDay");
    root.querySelector(".range").textContent = [this._format(this._start), this._format(this._end)].filter(Boolean).join(" – ");
    root.querySelector(".cancel").textContent = this._text("cancelRange");
    const apply = root.querySelector(".apply");
    apply.textContent = this._text("applyRange");
    apply.disabled = !(this._start && this._end && this._end >= this._start);
    const days = root.querySelector(".days");
    days.replaceChildren();
    for (let i=0; i<7; i++) {
      const label = document.createElement("span");
      label.className = "weekday";
      label.textContent = new Intl.DateTimeFormat(language, {weekday:"short", timeZone:"UTC"})
        .format(new Date(Date.UTC(2024,0,1+i,12)));
      days.append(label);
    }
    const offset = (this._month.getUTCDay()+6)%7;
    for (let i=0;i<offset;i++) days.append(document.createElement("span"));
    const year = this._month.getUTCFullYear(), month = this._month.getUTCMonth();
    const count = new Date(Date.UTC(year,month+1,0)).getUTCDate();
    for (let day=1;day<=count;day++) {
      const iso = this._iso(new Date(Date.UTC(year,month,day,12)));
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = String(day);
      button.setAttribute("aria-label", this._format(iso));
      if (iso === this._start || iso === this._end) button.className = "edge";
      else if (this._start && this._end && iso > this._start && iso < this._end) button.className = "inside";
      button.addEventListener("click", () => {
        if (!this._selectEnd || iso < this._start) {
          this._start = iso; this._end = ""; this._selectEnd = true;
        } else {
          this._end = iso; this._selectEnd = false;
        }
        this._render();
      });
      days.append(button);
    }
  }
  _apply() {
    if (!this._start || !this._end || this._end < this._start ||
        !this._fields.holiday_start || !this._fields.holiday_end) return;
    for (const [field, value] of [["holiday_start", this._start], ["holiday_end", this._end]]) {
      const target = this._fields[field];
      target.value = value;
      target.dispatchEvent(new CustomEvent("value-changed", {
        detail:{value}, bubbles:true, composed:true,
      }));
    }
    this._refresh();
    this._dialog.close();
  }
}
if (!customElements.get("ha-selector-bosch-buderus-holiday-date-range")) {
  customElements.define("ha-selector-bosch-buderus-holiday-date-range", BoschBuderusHolidayDateRange);
}
