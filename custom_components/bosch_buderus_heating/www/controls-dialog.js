// A native form selector. Draft edits never invoke an appliance service.

// Whether the minimum lies on the step grid (a missing minimum counts as 0).
const onStepGrid = (minimum, step) => {
  const ticks = (Number(minimum) || 0) / step;
  return Math.abs(ticks - Math.round(ticks)) < 1e-7;
};

// The on and off values of a switch row: "on"/"off", "start"/"stop" or,
// on a room controller, "true"/"false", as its options name them.
const switchValues = (options) => {
  const values = (options || []).map((option) => option.value);
  for (const pair of [
    ["start", "stop"],
    ["true", "false"],
  ]) {
    if (pair.every((value) => values.includes(value))) return pair;
  }
  return ["on", "off"];
};

// The server sends the texts of every language that has a data file
// (i18n/<code>.json); the profile language picks one, else English.
const controlsLanguage = (language, texts, aliases) => {
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

class BoschBuderusControls extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._changes = {};
    this._rows = [];
    this._active = "";
    this._invalid = new Set();
    // A click into the form is a deliberate focus choice; never override it.
    this.addEventListener("pointerdown", () => (this._focusPending = false));
    // HA submits its outer form on Enter. Local navigation and
    // discard/refresh buttons must not submit an appliance draft.
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target.closest("button")) {
        event.stopPropagation();
      }
    });
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color)}
        *{box-sizing:border-box}
        .tabs{display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--divider-color);padding-bottom:10px}
        button{font:inherit;cursor:pointer;min-height:44px;border:0;border-radius:8px;padding:8px 12px;background:transparent;color:var(--primary-color)}
        button[aria-selected="true"]{background:var(--primary-color);color:var(--text-primary-color,#fff)}
        button:disabled,input:disabled,select:disabled{opacity:.5;cursor:default}
        button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}
        .field{display:grid;grid-template-columns:minmax(0,1fr) minmax(110px,.8fr);gap:12px;align-items:center;padding:12px 0}
        .field label{overflow-wrap:anywhere}
        .input{display:flex;align-items:center;gap:7px;min-width:0}
        input,select{font:inherit;font-size:16px;width:100%;min-width:0;min-height:48px;padding:10px;border:0;border-bottom:1px solid var(--secondary-text-color);border-radius:4px 4px 0 0;background:var(--input-fill-color,#f1f1f1);color:var(--primary-text-color);color-scheme:var(--ha-color-scheme,light)}
        input[type="checkbox"]{width:24px;min-height:24px;height:24px;margin:10px 0;accent-color:var(--primary-color)}
        .input:has(input[type="checkbox"]){justify-content:flex-end}
        .unit{color:var(--secondary-text-color);white-space:nowrap}
        h3{font:inherit;font-weight:600;margin:16px 0 0;padding:14px 0 2px;border-top:1px solid var(--divider-color)}
        .actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px;margin-top:16px}
        .status{color:var(--secondary-text-color);margin:12px 0;line-height:1.5}
        .error,.hint{color:var(--error-color)}
        .hint{grid-column:1/-1;margin:-6px 0 0}
        input[aria-invalid="true"],select[aria-invalid="true"]{border-bottom:2px solid var(--error-color)}
        [hidden]{display:none}
        @media(max-width:360px){.field{grid-template-columns:minmax(0,1fr) minmax(96px,.65fr);gap:8px}}
      </style>
      <div class="tabs" role="tablist"></div>
      <div class="panels"></div>
      <p class="status" role="status" aria-live="polite"></p>
      <p class="error" role="alert"></p>
      <div class="actions"><button type="button" class="discard"></button><button type="button" class="refresh"></button></div>`;
    this.shadowRoot.querySelector(".discard").addEventListener("click", () => {
      this._changes = {};
      this._invalid.clear();
      this._emit();
      this._render();
    });
    this.shadowRoot
      .querySelector(".refresh")
      .addEventListener("click", () => this._refresh());
  }
  connectedCallback() {
    // Progressed HA flows block native Escape dismissal. Use their own close
    // button so flow cleanup follows the same path as a click on the header X.
    this._dialog = this._ancestor("dialog-data-entry-flow");
    this._escape = (event) => {
      const flow = this._ancestor("step-flow-form");
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        event.isComposing ||
        !this.isConnected ||
        flow?.domain !== "bosch_buderus_heating" ||
        flow.step?.step_id !== "operate"
      )
        return;
      // Let an open native choice list consume Escape first.
      if (
        CSS.supports("selector(:open)") &&
        event.composedPath().some((node) => node.matches?.("select:open"))
      )
        return;
      const close = this._dialog?.shadowRoot?.querySelector(
        'ha-icon-button[data-dialog="close"]',
      );
      if (!close) return;
      event.preventDefault();
      event.stopPropagation();
      close.click();
    };
    this._dialog?.addEventListener("keydown", this._escape, true);
    this._focusPending = true;
    this._restoreFocus();
  }
  _restoreFocus() {
    // HA replaces the form after Apply and can leave focus on the page body.
    // Restore keyboard navigation once per new form, and only if no other
    // element has gained focus. Later state updates never move the focus.
    queueMicrotask(() => {
      if (
        !this._focusPending ||
        !this.isConnected ||
        !this._dialog?.shadowRoot?.querySelector("ha-dialog")?.open
      )
        return;
      if (this.ownerDocument.activeElement === this.ownerDocument.body)
        this.focus();
      // Retry later only while the form's buttons are still disabled.
      this._focusPending =
        this.ownerDocument.activeElement === this.ownerDocument.body;
    });
  }
  disconnectedCallback() {
    this._dialog?.removeEventListener("keydown", this._escape, true);
    this._dialog = undefined;
  }
  _ancestor(name) {
    for (
      let node = this.parentElement || this.getRootNode()?.host;
      node;
      node = node.parentElement || node.getRootNode()?.host
    ) {
      if (node.localName === name) return node;
    }
    return undefined;
  }
  get _language() {
    return controlsLanguage(
      this._hass?.language,
      this._config?.texts,
      this._config?.aliases,
    );
  }
  _text(key, values = {}) {
    const texts = this._config?.texts || {};
    const text = texts[this._language]?.[key] ?? texts.en?.[key] ?? "";
    return text.replace(/\{(\w+)\}/g, (_, name) => String(values[name]));
  }
  _label(labels) {
    return labels?.[this._language] || labels?.en || "";
  }
  set hass(value) {
    const before = this._hass?.language;
    this._hass = value;
    if (before !== value?.language) this._render();
  }
  set selector(value) {
    const config = value?.["bosch-buderus-controls"] || {};
    if (JSON.stringify(config) === JSON.stringify(this._config)) return;
    this._config = config;
    this._rows = config.rows || [];
    // HA creates a new selector for every step. The server names the tab to
    // keep open and the values it rejected.
    if (config.active) this._active = config.active;
    this._invalid = new Set(config.invalid || []);
    this._render();
  }
  set value(value) {
    const changes = value?.changes || {};
    if (JSON.stringify(changes) === JSON.stringify(this._changes)) return;
    this._changes = { ...changes };
    this._render();
  }
  get value() {
    // The open tab only tells the server where to continue after Apply.
    return { changes: { ...this._changes }, active: this._active };
  }
  set disabled(value) {
    // HA sets every property again on each state update; only react to a
    // real change.
    if (Boolean(value) === this._disabled) return;
    this._disabled = Boolean(value);
    this._setDisabled();
  }
  set required(_value) {}
  set label(_value) {}
  focus() {
    this.shadowRoot.querySelector("button")?.focus();
  }
  reportValidity() {
    for (const input of this.shadowRoot.querySelectorAll("input,select")) {
      // An unchanged value is the appliance's own, even if off the step grid.
      if (!Object.hasOwn(this._changes, input.dataset.key)) continue;
      if (!input.disabled && !input.checkValidity()) {
        const panel = input.closest('[role="tabpanel"]');
        this._selectTab(panel.dataset.group);
        return input.reportValidity();
      }
    }
    return true;
  }
  _emit() {
    this.dispatchEvent(
      new CustomEvent("value-changed", {
        detail: { value: this.value },
        bubbles: true,
        composed: true,
      }),
    );
    this._updateStatus();
  }
  _group(group) {
    if (group === "system") return this._text("system");
    const [family, id] = group.split("/");
    const number = id.match(/\d+$/)?.[0] || id;
    return (
      (family === "heatingCircuits"
        ? this._text("heatingCircuit")
        : this._text("hotWater")) +
      " " +
      number
    );
  }
  _selectTab(group, focus = false) {
    this._active = group;
    for (const tab of this.shadowRoot.querySelectorAll('[role="tab"]')) {
      const active = tab.dataset.group === group;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    }
    for (const panel of this.shadowRoot.querySelectorAll('[role="tabpanel"]'))
      panel.hidden = panel.dataset.group !== group;
  }
  _render() {
    if (!this.shadowRoot) return;
    const tabs = this.shadowRoot.querySelector(".tabs");
    const panels = this.shadowRoot.querySelector(".panels");
    tabs.replaceChildren();
    panels.replaceChildren();
    tabs.setAttribute(
      "aria-label",
      this._text("sections"),
    );
    const groups = [...new Set(this._rows.map((row) => row.group))];
    if (!groups.includes(this._active)) this._active = groups[0] || "";
    groups.forEach((group, index) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.role = "tab";
      tab.id = "tab-" + index;
      tab.dataset.group = group;
      tab.textContent = this._group(group);
      tab.setAttribute("aria-controls", "panel-" + index);
      tab.addEventListener("click", () => {
        this._selectTab(group);
        this._emit();
      });
      tab.addEventListener("keydown", (event) => {
        let target;
        if (event.key === "ArrowRight") target = (index + 1) % groups.length;
        if (event.key === "ArrowLeft")
          target = (index - 1 + groups.length) % groups.length;
        if (event.key === "Home") target = 0;
        if (event.key === "End") target = groups.length - 1;
        if (target !== undefined) {
          event.preventDefault();
          this._selectTab(groups[target], true);
          this._emit();
        }
      });
      tabs.append(tab);
      const panel = document.createElement("div");
      panel.role = "tabpanel";
      panel.id = "panel-" + index;
      panel.dataset.group = group;
      panel.setAttribute("aria-labelledby", tab.id);
      const advanced = document.createElement("section");
      const heading = document.createElement("h3");
      heading.id = "advanced-" + index;
      advanced.setAttribute("aria-labelledby", heading.id);
      heading.textContent = this._text("moreSettings");
      advanced.append(heading);
      this._rows
        .filter((row) => row.group === group)
        .forEach((row, rowIndex) => {
          const field = document.createElement("div");
          field.className = "field";
          const label = document.createElement("label");
          const id = "control-" + index + "-" + rowIndex;
          label.htmlFor = id;
          label.textContent = this._label(row.label);
          const box = document.createElement("div");
          box.className = "input";
          const input = document.createElement(
            row.kind === "select" ? "select" : "input",
          );
          input.id = id;
          input.dataset.key = row.key;
          input.dataset.available = String(row.available);
          const value = Object.hasOwn(this._changes, row.key)
            ? this._changes[row.key]
            : row.value;
          if (row.kind === "number") {
            input.type = "number";
            input.min = row.minimum;
            input.max = row.maximum;
            // Arrow keys and the spinner follow the control's step; without
            // a known step any value within the bounds can be entered. The
            // browser counts the step from the minimum: with a minimum off
            // the step grid it would reject values the appliance accepts, so
            // any value is entered then and the server checks it.
            input.step =
              row.step > 0 && onStepGrid(row.minimum, row.step)
                ? String(row.step)
                : "any";
            input.inputMode = "decimal";
            input.required = true;
            input.value = value;
            const unit = document.createElement("span");
            unit.className = "unit";
            unit.textContent = row.unit;
            box.append(input, unit);
          } else if (row.kind === "switch") {
            input.type = "checkbox";
            input.role = "switch";
            input.checked = value === switchValues(row.options)[0];
            box.append(input);
          } else {
            for (const choice of row.options) {
              const option = document.createElement("option");
              option.value = choice.value;
              option.textContent = this._label(choice.label);
              input.append(option);
            }
            input.value = value;
            box.append(input);
          }
          let hint;
          if (this._invalid.has(row.key)) {
            hint = document.createElement("p");
            hint.className = "hint";
            hint.id = id + "-hint";
            hint.textContent = this._text("checkValue");
            input.setAttribute("aria-invalid", "true");
            input.setAttribute("aria-describedby", hint.id);
          }
          input.addEventListener("input", () => {
            if (this._invalid.delete(row.key)) {
              input.removeAttribute("aria-invalid");
              input.removeAttribute("aria-describedby");
              hint?.remove();
            }
            const selected =
              row.kind === "switch"
                ? switchValues(row.options)[input.checked ? 0 : 1]
                : row.kind === "number"
                  ? input.value === ""
                    ? null
                    : Number(input.value)
                  : input.value;
            if (selected === row.value) delete this._changes[row.key];
            else this._changes[row.key] = selected;
            this._emit();
          });
          field.append(label, box);
          if (hint) field.append(hint);
          (row.advanced ? advanced : panel).append(field);
        });
      if (advanced.children.length > 1) panel.append(advanced);
      panels.append(panel);
    });
    this._selectTab(this._active);
    this.shadowRoot.querySelector(".discard").textContent =
      this._text("discard");
    this.shadowRoot.querySelector(".refresh").textContent = this._text("reload");
    this._updateStatus();
    this._setDisabled();
  }
  _updateStatus() {
    const count = Object.keys(this._changes).length;
    this.shadowRoot.querySelector(".status").textContent = count
      ? this._text("unsaved", { count })
      : this._config?.status === "saved"
        ? this._text("saved")
        : this._config?.status === "empty"
          ? this._text("empty")
          : this._text("pending");
  }
  _setDisabled() {
    for (const element of this.shadowRoot.querySelectorAll(
      "input,select,button",
    ))
      element.disabled =
        this._disabled || this._busy || element.dataset.available === "false";
    this._restoreFocus();
  }
  async _refresh() {
    if (this._busy || this._disabled) return;
    const flow = this._ancestor("step-flow-form");
    const error = this.shadowRoot.querySelector(".error");
    if (
      flow?.domain !== "bosch_buderus_heating" ||
      flow.step?.step_id !== "operate" ||
      !flow.flowConfig?.handleFlowStep
    ) {
      error.textContent = this._text("reopen");
      return;
    }
    this._busy = true;
    this._setDisabled();
    const fire = (name, detail) =>
      this.dispatchEvent(
        new CustomEvent(name, { detail, bubbles: true, composed: true }),
      );
    fire("flow-step-footer-state-changed", { loading: true });
    try {
      const step = await flow.flowConfig.handleFlowStep(
        flow.hass,
        flow.step.flow_id,
        { controls: { action: "refresh", active: this._active } },
      );
      if (this.isConnected) {
        this._changes = {};
        fire("flow-update", { step });
      }
    } catch {
      error.textContent = this._text("reloadFailed");
    } finally {
      this._busy = false;
      this._setDisabled();
      if (this.isConnected)
        fire("flow-step-footer-state-changed", { loading: false });
    }
  }
}
if (!customElements.get("ha-selector-bosch-buderus-controls")) {
  customElements.define(
    "ha-selector-bosch-buderus-controls",
    BoschBuderusControls,
  );
}
