# Translations pending for the RRC work packages

A work package (W0 to W6) adds its new texts in German and English only:
keys in `strings.json`, `translations/en.json` and `translations/de.json`,
and built texts next to the code (`localize`, contract `names` and `texts`).
The other languages follow in one step at the end (W7).

Until then each package lists what may still be missing in the other
languages in its own file `W<n>.json`:

```json
{
  "package": "W1",
  "keys": ["entity.climate.rrc_zone.name"],
  "texts": ["Temporary setpoint ends at the next switch point"]
}
```

- `keys`: flattened keys of `strings.json` (dots between the levels).
- `texts`: English source texts of built texts (the keys of `i18n/*.json`).

`tests/test_translations.py` lets these keys and texts be missing in the
other languages and leaves them out of the order check. Another test makes
sure every listed key and text exists in the source. Each package owns only
its own file; W7 translates the entries and deletes the files.

To merge the three translation files of two packages, use the JSON deep
merge instead of a text merge:

```
python scripts/merge_translation_keys.py --git-conflict custom_components/bosch_buderus_heating/strings.json
```
