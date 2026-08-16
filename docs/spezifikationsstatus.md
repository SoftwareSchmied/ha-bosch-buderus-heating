# Umsetzungsstand der Projektspezifikation

Dieses Dokument verfolgt die schrittweise Umsetzung der extern abgelegten
Projektspezifikation. Die Quelldatei selbst gehört bewusst nicht zum
Repository. Der Status beschreibt implementierten und geprüften Code, nicht
nur geplante Architektur.

## Bewusste Abweichungen

- Energie wird alle 5 statt alle 10 Minuten gelesen.
- Statische Werte werden nur bei Discovery gelesen und nicht zusätzlich nach
  24 Stunden.

Diese beiden Abweichungen sind für das Projekt akzeptiert.

## Status nach Themenblock

| Themenblock | Status | Wesentliche offene Punkte |
|---|---|---|
| Repository, Paketstruktur und CI | umgesetzt | Preview-ZIP lokal reproduzierbar gebaut und isoliert importiert; HACS/hassfest laufen zusätzlich vor Veröffentlichung im Release-Workflow |
| OAuth/PKCE und Tokenrotation | weitgehend umgesetzt | realer Bosch- und realer Reauth-Test |
| Dynamische Discovery | umgesetzt | Reconfigure und capability-basierte Firmware-Schemaprüfung vorhanden; weitere reale Profile beobachten |
| Polling und Teilfehler | weitgehend umgesetzt | Standard- und cloud-schonendes Profil vorhanden; Gateway-Jitter offen |
| Negative Fähigkeiten | umgesetzt | aktive Pausen werden aggregiert diagnostiziert |
| Zustands- und Verfügbarkeitsmodell | umgesetzt | geschwärzte Diagnostics vorhanden |
| Read-only-Entitäten | auf einem K40 umgesetzt | Reifegrade und Standardaktivierung umgesetzt; weitere Anlagenprofile offen |
| Energie | weitgehend umgesetzt | direkte Zähler, berechnete Umweltenergie und Reset-Erkennung umgesetzt; Langzeittest offen |
| Diagnostics/Supportexport | umgesetzt | verständliche HTTP- und Capability-Erfolgszähler vorhanden; weitere reale Supportfälle beobachten |
| Reconfigure und Repairs | umgesetzt | Gatewaywahl, Markenwechsel, Rediscovery, Pollingprofile, Rate-Limit- und Firmware-Schema-Repair vorhanden |
| Schreibdienst und Steuerungen | weitgehend umgesetzt | sichere Benutzersteuerungen dynamisch freigegeben und auf der Referenzanlage punktuell real getestet; weitere Anlagenprofile offen |
| Entwickler-CLI | weitgehend umgesetzt | anonymisierte Inventur aus HA-Diagnostics vorhanden; automatische Contract-Fixture-Erzeugung bei Bedarf später |
| Preview-Gate 0.1.0 | lokal umgesetzt | Veröffentlichung bleibt eine ausdrückliche separate Freigabe |
| Release-Gate 1.0 | offen | mehrere Profile, Bosch, Dauerlauf und weitere reale Schreibtests |

## Nächste Reihenfolge

1. Weitere reale Bosch-/Buderus-Anlagenprofile und anonymisierte Inventuren.
2. Dauerlauf nach Deployment inklusive Tokenrotation, Cloudausfällen und Rate-Limits.
