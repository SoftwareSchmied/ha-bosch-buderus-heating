// A read-only weekly view of a time program inside the options dialog.
// It never writes and never fetches: the server sends the days, segments
// and level names already shown by the dialog. Texts come from the server
// for every language with a data file (i18n/<code>.json); the profile
// language picks one, else English. Device texts go in as text only.
const scheduleLanguage = (language, texts, aliases) => {
  const tag = String(language || "").trim().toLowerCase().replace("_", "-");
  const base = tag.split("-")[0];
  const known = new Map(
    Object.keys(texts || {}).map((code) => [code.toLowerCase(), code]),
  );
  for (const candidate of [tag, aliases?.[tag], base, aliases?.[base]]) {
    if (candidate && known.has(candidate)) return known.get(candidate);
  }
  return "en";
};

const clock = (minute) => {
  const hours = String(Math.floor(minute / 60)).padStart(2, "0");
  const minutes = String(minute % 60).padStart(2, "0");
  return `${hours}:${minutes}`;
};

const KNOWN = new Set([
  "comfort", "comfort1", "comfort2", "comfort3", "eco", "eco_plus", "low",
  "high", "off", "on", "min", "red", "nom", "max", "dem",
]);

// The colour class of a level: a known key, a temperature band or none.
const levelClass = (level) => {
  if (level === null || level === undefined) return "none";
  if (KNOWN.has(level)) return `l-${level}`;
  const number = Number(level);
  if (Number.isFinite(number) && String(level).trim() !== "") {
    if (number <= 0) return "l-off";
    if (number < 17) return "t1";
    if (number < 19.5) return "t2";
    if (number < 21.5) return "t3";
    if (number < 23.5) return "t4";
    return "t5";
  }
  return "unknown";
};

const STYLE = `
  :host { display: block; }
  .wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; min-width: 360px; }
  caption { text-align: start; color: var(--secondary-text-color, #5f5f5f); padding-bottom: 6px; }
  th { font-weight: 500; text-align: start; padding: 2px 8px 2px 0; white-space: nowrap;
       color: var(--primary-text-color, #212121); }
  th[scope="row"] { position: sticky; left: 0; background: var(--card-background-color, #fff); z-index: 1; }
  td { width: 100%; padding: 2px 0; }
  .ruler { display: flex; justify-content: space-between; font-size: 12px;
           color: var(--secondary-text-color, #5f5f5f); }
  .bar { display: flex; height: 28px; border: 1px solid var(--divider-color, #8a8a8a);
         border-radius: 4px; overflow: hidden; }
  .seg { box-sizing: border-box; height: 100%; min-width: 3px; overflow: hidden;
         white-space: nowrap; text-overflow: clip; font-size: 12px; line-height: 28px;
         padding: 0 2px; color: #111; border-left: 1px solid rgba(0, 0, 0, .55); }
  .seg:first-child { border-left: 0; }
  .seg.point { border-left: 3px solid #111; }
  .seg.carried { background-image: repeating-linear-gradient(90deg, transparent 0 6px,
                 rgba(0, 0, 0, .12) 6px 8px); }
  .seg:focus-visible { outline: 3px solid var(--primary-color, #03a9f4); outline-offset: -3px; }
  .l-comfort, .l-comfort1, .l-comfort2, .l-comfort3, .l-high, .l-on, .l-max { background-color: #f6b26b; }
  .l-eco, .l-low, .l-red, .l-min { background-color: #9fc5e8; }
  .l-eco_plus, .l-nom, .l-dem { background-color: #b6d7a8; }
  .l-off { background-color: #d9d9d9; }
  .t1 { background-color: #9fc5e8; } .t2 { background-color: #c9daf8; }
  .t3 { background-color: #ffe599; } .t4 { background-color: #f9cb9c; }
  .t5 { background-color: #ea9999; }
  .unknown { background-color: #eeeeee;
             background-image: repeating-linear-gradient(45deg, transparent 0 5px, rgba(0, 0, 0, .25) 5px 7px); }
  .none { background-color: transparent; color: var(--secondary-text-color, #5f5f5f); font-style: italic; }
  tr.changed th[scope="row"]::after { content: " *"; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
`;

class BoschBuderusScheduleWeek extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    this._root = document.createElement("div");
    this._root.className = "wrap";
    this.shadowRoot.append(style, this._root);
  }

  set selector(value) {
    this._config = value?.["bosch-buderus-schedule-week"] || {};
    this._render();
  }

  set hass(value) {
    const language = value?.language;
    this._hass = value;
    if (language !== this._language) {
      this._language = language;
      this._render();
    }
  }

  // Read-only: the field keeps whatever value the form holds.
  set value(_value) {}

  get value() { return undefined; }

  _text(key) {
    const texts = this._config?.texts || {};
    const code = scheduleLanguage(this._hass?.language, texts, this._config?.aliases);
    return texts[code]?.[key] ?? texts.en?.[key] ?? "";
  }

  _label(day, segment) {
    const level = segment.level === null || segment.level === undefined
      ? this._text("noLevel")
      : String(segment.label ?? segment.level);
    let text = this._text("segment")
      .replace("{day}", String(day.name ?? ""))
      .replace("{start}", clock(segment.start))
      .replace("{end}", clock(segment.end))
      .replace("{level}", level);
    if (segment.carried) text += ` (${this._text("carried")})`;
    return text;
  }

  _render() {
    const days = Array.isArray(this._config?.days) ? this._config.days : [];
    this._root.replaceChildren();
    if (!days.length) return;
    const table = document.createElement("table");
    const caption = document.createElement("caption");
    caption.textContent = `${this._text("title")}. ${this._text("clock")}`;
    table.append(caption);
    const head = table.createTHead().insertRow();
    const dayHead = document.createElement("th");
    dayHead.scope = "col";
    dayHead.textContent = this._text("day");
    const hourHead = document.createElement("th");
    hourHead.scope = "col";
    hourHead.setAttribute("aria-label", this._text("hours"));
    const ruler = document.createElement("div");
    ruler.className = "ruler";
    ruler.setAttribute("aria-hidden", "true");
    for (const hour of [0, 6, 12, 18, 24]) {
      const tick = document.createElement("span");
      tick.textContent = String(hour);
      ruler.append(tick);
    }
    hourHead.append(ruler);
    head.append(dayHead, hourHead);
    const body = table.createTBody();
    const segments = [];
    for (const day of days) {
      const row = body.insertRow();
      if (day.changed) {
        row.className = "changed";
        row.title = this._text("changed");
      }
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = String(day.name ?? "");
      const cell = row.insertCell();
      const bar = document.createElement("div");
      bar.className = "bar";
      for (const segment of Array.isArray(day.segments) ? day.segments : []) {
        const span = document.createElement("span");
        const start = Number(segment.start) || 0;
        const end = Number(segment.end) || 0;
        span.className = `seg ${levelClass(segment.level)}`;
        if (segment.point) span.classList.add("point");
        if (segment.carried) span.classList.add("carried");
        span.style.flex = `0 0 ${((end - start) / 1440) * 100}%`;
        const label = day.empty && (segment.level === null || segment.level === undefined)
          ? `${day.name ?? ""}: ${this._text("empty")}`
          : this._label(day, segment);
        span.setAttribute("aria-label", label);
        span.title = label;
        span.tabIndex = -1;
        // The text, not the colour, carries the meaning.
        span.textContent = day.empty && (segment.level === null || segment.level === undefined)
          ? this._text("empty")
          : String(segment.label ?? segment.level ?? "");
        bar.append(span);
        segments.push(span);
      }
      cell.append(bar);
      row.prepend(name);
    }
    if (segments.length) segments[0].tabIndex = 0;
    table.addEventListener("keydown", (event) => this._move(event, segments));
    this._root.append(table);
  }

  // Arrow keys move between segments; Tab leaves the view (no focus trap).
  _move(event, segments) {
    const index = segments.indexOf(this.shadowRoot.activeElement);
    if (index < 0) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = segments[Math.min(segments.length - 1, Math.max(0, index + step))];
    segments[index].tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  }
}

if (!customElements.get("ha-selector-bosch-buderus-schedule-week")) {
  customElements.define("ha-selector-bosch-buderus-schedule-week", BoschBuderusScheduleWeek);
}
