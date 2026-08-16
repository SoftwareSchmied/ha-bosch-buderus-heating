# Anonymisierte Anlageninventur

Eine anonymisierte Inventur hilft dabei, weitere Bosch- und Buderus-Anlagen zu
unterstützen, ohne Zugriff auf das Heizkonto oder die Anlage zu teilen. Das
Werkzeug arbeitet ausschließlich mit dem bereits geschwärzten
Home-Assistant-Diagnosebericht und führt keine Cloud-Abfrage aus.

## 1. Diagnosebericht herunterladen

1. Öffne **Einstellungen → Geräte & Dienste**.
2. Öffne **Bosch/Buderus Heating**.
3. Öffne das Menü mit den drei Punkten.
4. Wähle **Diagnosedaten herunterladen**.
5. Prüfe die Datei trotzdem selbst, bevor du sie weitergibst.

## 2. Kleinere Inventur erzeugen

Führe im ausgecheckten Projektverzeichnis aus:

```bash
python -m custom_components.bosch_buderus_heating.inventory \
  config_entry-bosch_buderus_heating.json \
  pointt-inventory.json
```

Eine vorhandene Zieldatei wird nicht überschrieben. Falls das bewusst
gewünscht ist, ergänze `--force`.

## Inhalt

Die Inventur enthält nur:

- anonyme Gatewayklasse wie `k40` oder `mx400`;
- Pfadschablonen mit `{hc}`, `{dhw}` und `{hs}`;
- Datentypen, Einheiten, Pollinggruppen und Reifegrade;
- Schreibbarkeit sowie das Vorhandensein von Grenzen oder Auswahlwerten;
- aggregierte Anzahlen je Capability-Klasse.

Verworfen werden insbesondere Rohwerte, Laufzeit- und Request-Metriken,
Anzeigenamen, Gateway- und Config-Entry-IDs, Seriennummern, Firmwarestrings,
Token, Netzwerkdaten und benutzerdefinierte Namen. Das Werkzeug bricht ab,
wenn der Diagnosebericht nicht ausdrücklich alle Datenschutzmerkmale als
geschwärzt ausweist oder ein dynamischer Pfad noch eine konkrete Anlagen-ID
enthält.

## Firmwarewarnung

Nach jeder vollständigen Erkennung vergleicht die Integration vorhandene,
bekannte PointT-Pfade mit ihren sicheren Datentypen und Einheiten. Eine neue
Firmwareversion allein löst keine Warnung aus. Nur eine echte Schemaabweichung
erzeugt unter **Reparaturen** den Hinweis **PointT-Fähigkeiten haben sich nach
einem Firmwareupdate geändert**. Die Reparatur lädt die Integration neu und
prüft alle Fähigkeiten erneut. Bleibt die Warnung bestehen, ist die
anonymisierte Inventur die passende Grundlage für einen Fehlerbericht.
