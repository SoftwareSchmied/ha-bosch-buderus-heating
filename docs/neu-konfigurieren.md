# Neu konfigurieren und Ressourcen erneut suchen

Bosch/Buderus Heating erkennt die Ressourcen der ausgewählten Heizungsanlage
bei jeder Einrichtung und bei jedem Neuladen der Integration dynamisch. Eine
erneute Suche ist beispielsweise nach einem Firmwareupdate oder nach einer
Änderung an Heizkreisen, Warmwasserbereitung, Wärmeerzeugern oder Gateways
sinnvoll.

## Konfiguration öffnen

1. Öffne in Home Assistant **Einstellungen → Geräte & Dienste**.
2. Suche **Bosch/Buderus Heating**.
3. Öffne das Menü mit den drei Punkten auf der Integrationskarte.
4. Wähle **Neu konfigurieren**.

Home Assistant lädt zuerst die aktuelle Gateway-Liste aus der PointT-Cloud.
Im folgenden Dialog lassen sich App-Marke, verwendete Gateways und das
Abfrageprofil kontrollieren.

## Vollständige Suche ausführen

Wähle **Senden**, auch wenn keine Einstellung geändert werden soll. Die
Integration wird neu geladen und liest den Ressourcenbaum der ausgewählten
Gateways vollständig neu ein. Neu hinzugekommene, unterstützte Werte werden als
Entitäten angelegt. Registry-Einträge absichtlich abgewählter Gateways werden
entfernt.

Beim Wechsel zwischen Bosch und Buderus ist eine neue SingleKey-ID-Anmeldung
erforderlich, weil beide Apps unterschiedliche OAuth-Konfigurationen verwenden.

## Abfrageprofile

| Profil | Betriebswerte | Steuerung und Energie | Langsame Zähler |
|---|---:|---:|---:|
| Standard | 1 Minute | 5 Minuten | 15 Minuten |
| Cloud-schonend | 2 Minuten | 10 Minuten | 30 Minuten |

Statische Informationen werden bei der Discovery gelesen und danach nicht
regelmäßig abgefragt. Beide Profile verwenden Batch-Abfragen, Teilfehler,
Backoff, negative Zwischenspeicherung und einen Circuit Breaker. Das
cloud-schonende Profil ist für Anlagen gedacht, bei denen die PointT-Cloud
wiederholt Rate-Limits meldet.

## Reparaturhinweise

Nach drei Rate-Limit-Ereignissen innerhalb einer laufenden Home-Assistant-Sitzung
erstellt die Integration unter **Einstellungen → System → Reparaturen** einen
Hinweis. Der angebotene Reparaturdialog kann das betroffene Konto auf das
cloud-schonende Profil umstellen und die Integration anschließend neu laden.

Ungültige Zugangsdaten werden nicht als Rate-Limit behandelt. Dafür verwendet
Home Assistant den getrennten Vorgang **Erneut authentifizieren**.
