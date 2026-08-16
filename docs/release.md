# Release durchführen

Releases werden aus einem geprüften Git-Tag gebaut. Das HACS-Archiv enthält die
Integration direkt an seiner Wurzel und wird zusammen mit einer SHA-256-Datei
veröffentlicht.

## Lokale Freigabeprüfung

Im Repository ausführen:

```bash
ruff format --check .
ruff check .
mypy
pytest
python -m pip_audit --strict
python scripts/build_release.py --expected-version 0.1.0
```

Die offizielle HACS- und hassfest-Validierung benötigt Docker. Ist Docker lokal
nicht verfügbar, bleibt die Veröffentlichung trotzdem gesperrt: Der
taggebundene GitHub-Workflow führt beide Prüfungen aus, bevor er ein Release
anlegt.

Danach müssen `dist/bosch_buderus_heating.zip` und
`dist/bosch_buderus_heating.zip.sha256` vorhanden sein. Das ZIP lässt sich zum
Gegenprüfen in einen leeren Ordner entpacken; `manifest.json` muss direkt auf
der ersten Ebene liegen.

## Veröffentlichung

1. Versionsnummer in `manifest.json` und Überschrift in `CHANGELOG.md` müssen
   übereinstimmen.
2. Der Arbeitsbaum muss sauber sein und alle lokalen Prüfungen müssen bestehen.
3. Erst nach ausdrücklicher Freigabe den geprüften Stand zu GitHub übertragen.
4. Anschließend den signierten oder annotierten Tag `v0.1.0` erstellen und
   übertragen.

Der Tag startet den Release-Workflow. Dieser wiederholt Tests, Typ- und
Stilprüfung, Abhängigkeitsprüfung, hassfest und HACS-Validierung. Nur danach
wird die GitHub-Prerelease mit ZIP und Prüfsumme angelegt.

## Grenzen der Preview 0.1.0

Die Preview ist auf einer Buderus-Anlage mit K40 real geprüft. Bosch-Anlagen,
weitere Gatewaymodelle, mehrere Kreise und der Langzeitbetrieb sind noch nicht
ausreichend belegt. Diese Einschränkungen bleiben im README und in der Roadmap
sichtbar; `0.1.0` ist keine stabile `1.0`.
