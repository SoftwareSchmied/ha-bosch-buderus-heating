# Diagnosedaten und Request-Metriken

Home Assistant kann für Bosch/Buderus Heating einen Diagnosebericht erzeugen.
Er ist für Fehlersuche und Support gedacht und führt keine zusätzlichen
Cloud-Abfragen aus.

## Diagnosebericht herunterladen

1. Öffne **Einstellungen → Geräte & Dienste**.
2. Öffne **Bosch/Buderus Heating**.
3. Öffne das Menü mit den drei Punkten der Integration.
4. Wähle **Diagnosedaten herunterladen**.

Prüfe die Datei trotzdem vor einer Veröffentlichung. Die automatische
Schwärzung ist eine zusätzliche Schutzschicht und ersetzt nicht die eigene
Kontrolle vor dem Anhängen an ein öffentliches Issue.

## Enthaltene Informationen

- ausgewählte Marke und Anzahl eingerichteter Gateways;
- anonymisierte Geräteklasse wie `k40`, `k40rf`, `mx300` oder `mx400`;
- normalisierte Ressourcenpfade ohne konkrete Heizkreis-, Warmwasser- oder
  Wärmeerzeuger-ID;
- Ressourcentyp, Einheit, Polling-Gruppe, Reifegrad, Standardaktivierung und
  Schreibbarkeitsangabe;
- Anzahl erlaubter Optionen, Referenzen und strukturierter Teilwerte;
- Verfügbarkeit, Freshness, Fehlerkategorie und Anzahl aufeinanderfolgender
  Fehler;
- Anzahl aktiver negativer Pausen, Rate-Limit-Backoff und Circuit-Breaker;
- aggregierte Request- und Polling-Metriken.

Der Bericht enthält ausdrücklich keine aktuellen Mess-, Einstellungs- oder
Energiewerte.

## Request-Metriken

Die Metriken werden nur im Arbeitsspeicher gezählt und bei einem Neustart von
Home Assistant zurückgesetzt. Erfasst werden:

- tatsächliche HTTP-Versuche nach Kategorie und Methode;
- HTTP-Statusklassen wie `2xx`, `4xx` oder `5xx`;
- Ergebnisse wie Erfolg, Timeout, Rate-Limit oder Protokollfehler;
- Retries und begrenzte Einzelabfrage-Fallbacks;
- Anzahl und maximale Größe der Batch-Abfragen;
- erfolgreiche und fehlgeschlagene Elemente innerhalb von Batch-Antworten;
- letzte, durchschnittliche und maximale Request-Dauer;
- Anzahl, Fehler und Dauer der Coordinator-Polls;
- Anzahl erkannter Rücksprünge kumulierter Energiezähler.

Es werden dafür weder URLs noch Ressourcenpfade, Gateway-IDs, Payloads oder
Antwortwerte gespeichert.

### Einfache Gesamtübersicht

Unter `request_metrics` stehen direkt verständliche Summen:

- `observation_seconds`: Zeitraum seit dem Start der Integration;
- `requests_total`: tatsächlich ausgeführte Cloud-Anfragen;
- `requests_successful` und `requests_failed`: erfolgreiche und fehlgeschlagene
  Cloud-Anfragen;
- `success_rate_percent`: Erfolgsquote in Prozent;
- `requests_per_hour`: auf eine Stunde hochgerechnete Cloudlast. Dieser Wert
  wird erst nach einer Beobachtungszeit von 60 Sekunden berechnet;
- `rate_limit_events`: Anzahl der von PointT gemeldeten Begrenzungen.

### Zähler je Wert

Jeder Eintrag unter `gateways → capabilities` enthält einen Abschnitt `calls`.
Er zeigt beispielsweise:

```json
{
  "name": "Außentemperatur",
  "calls": {
    "attempts_total": 120,
    "successful": 119,
    "failed": 1,
    "success_rate_percent": 99.2,
    "results": {
      "success": 119,
      "timeout": 1
    },
    "last_result": "success"
  }
}
```

Ein „Call“ in diesem Abschnitt ist der Versuch, genau diesen Wert innerhalb
einer Sammelabfrage zu lesen. Ein einziger HTTP-Aufruf kann bis zu 30 solcher
Wert-Abfragen enthalten. Die Gesamtzahl echter Cloud-Anfragen steht deshalb
getrennt unter `request_metrics`.

Mögliche Ergebnisse sind unter anderem `success`, `not_found`, `forbidden`,
`timeout`, `rate_limited`, `service_unavailable`, `authentication_error` und
`request_failed`. Absichtlich pausierte oder noch nicht fällige Werte werden
nicht als fehlgeschlagene Abfrage gezählt.

Die Zähler entstehen aus den ohnehin ausgeführten Abrufen. Diagnostics löst
keine zusätzliche Cloud-Anfrage aus. Nach einem Neustart beginnen sämtliche
Zähler wieder bei null.

`energy_counter_resets_detected` steigt, wenn ein einzelner nicht negativer
PointT-Energiezähler gegenüber seinem vorherigen bestätigten Wert sinkt. Es
enthält weder den alten noch den neuen Messwert und dient nur dazu, Resets nach
Firmwareupdates, Gerätetausch oder Zurücksetzen nachvollziehen zu können.

## Nicht enthaltene Informationen

- Access- oder Refresh-Token, OAuth-Code und Redirect-Adresse;
- Gateway-ID, Config-Entry-ID, Seriennummer oder UUID;
- IP-Adresse, MAC-Adresse, SSID oder Standort;
- Firmwarekennung und vollständige Modellbezeichnung;
- benutzerdefinierte Heizkreis-, Warmwasser- oder Zeitprogrammnamen;
- aktuelle Temperaturen, Sollwerte, Betriebsarten oder Energieverbräuche;
- vollständige Anfrage- oder Antwortkörper.
