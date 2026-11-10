// The dialog texts the server sends, read from the integration's data files.
const fs = require("node:fs");
const path = require("node:path");

const I18N = path.join(
  __dirname,
  "../../custom_components/bosch_buderus_heating/i18n",
);

// The same aliases as ALIASES in i18n/__init__.py.
const ALIASES = { no: "nb", nn: "nb" };

const dialogConfig = (name) => ({
  texts: Object.fromEntries(
    fs
      .readdirSync(I18N)
      .filter((file) => file.endsWith(".json"))
      .map((file) => [
        file.slice(0, -".json".length),
        JSON.parse(fs.readFileSync(path.join(I18N, file), "utf8")).dialogs?.[
          name
        ],
      ])
      .filter(([, texts]) => texts),
  ),
  aliases: ALIASES,
});

module.exports = { dialogConfig };
