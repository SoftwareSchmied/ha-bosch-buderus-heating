# Home Assistant device presentation

The shared-device decision below remains in effect. The **Beta.7**
[heating controls](heating-controls.md) and [PointT hot water](pointt-hot-water.md)
add combined climate and hot-water entities to that same device. Individual
control labels are shorter, numeric controls use input fields, and the original
controls for the heating mode, manual target and hot-water mode move to Configuration.
Existing entity identities and user customizations remain intact.

Die folgende Entscheidung für ein gemeinsames Anlagengerät gilt weiterhin.
Die mit **Beta.7 eingeführte** [Heizungsbedienung](heating-controls.md) und
[PointT-Warmwasserentität](pointt-hot-water.md) ergänzen gemeinsame Klima- und
Warmwasserentitäten am selben Gerät. Beschriftungen werden kürzer,
Zahlenfelder ersetzen Schieberegler. Die ursprünglichen Einzelregler für
Heizkreis-Betriebsart, manuellen Sollwert und Warmwasser-Betriebsart stehen
unter Konfiguration. Entitätskennungen und eigene Anpassungen bleiben erhalten.

## English

I keep one shared device per PointT heating gateway. I will create additional
device pages only for objects that PointT actually identifies as devices,
with verified identity and resource ownership. Heating, hot-water and solar
circuits remain functional groups unless separate device evidence exists.
The [plant model](architecture.md#plant-model-and-device-evidence) now makes
that distinction explicit and supplies the current central device binding.

Related entities remain recognizable by their default name prefixes, such as
Heating circuit 1, Hot water 1 and Heat generator 1. The integration uses
reported circuit identities and configured circuit names where available.
Custom entity names in Home Assistant continue to take precedence. Several
gateways remain separate even if their local circuit numbers are identical.

This decision does not add, remove or move entities. Existing entity IDs,
unique IDs, history, disabled states, user names, areas, labels and device
references stay as they are. There is no new registry migration or layout
option. The existing compatibility migrations for older previews remain.

An entity can still be assigned to its own Home Assistant area when a circuit
needs a different area from the shared device. Such an explicit entity area
overrides the device area; the integration does not set those overrides.
Dashboard cards can group entities without changing their device assignment.

Shared system readings, notifications, holiday calendars and derived values
stay associated with the gateway as before. Account-wide request statistics
remain independent of a particular device. A central heat-source reading is
not automatically attributed to one generator merely because only one
generator currently appears in discovery.

Independent appliances connected through the additional cloud adapter, and
separate PointT wallboxes, keep their own device identities. The decision
does not merge those appliances into the heating gateway.

### If PointT identifies an additional device

A new device needs evidence from the PointT device model, a stable identifier
and an explicit assignment of its resources. A circuit key, module name or
resource path alone is insufficient. Additional circuits, solar functions and
other supported resource groups can extend the inventory at the same gateway.
The current model does not infer new devices or additional measurements.

A future implementation needs an explicit, reversible assignment migration.
Preserving entity IDs alone is insufficient: actions targeting a device can
select a different entity set after a split, and inherited areas or labels
may change. The parent link between devices does not automatically transfer
the parent's area, labels or automation targets. A migration must explain
these effects, preserve user changes and handle temporary device absence.

No split or migration has been implemented as part of this decision.

## Deutsch

Ich behalte ein gemeinsames Gerät je PointT-Heizungs-Gateway bei. Zusätzliche
Geräteseiten lege ich nur für Objekte an, die PointT tatsächlich als Geräte
führt und deren Identität und Ressourcenzuordnung belegt sind. Heiz-,
Warmwasser- und Solarkreise bleiben ohne eigenen Gerätenachweis Funktionsgruppen.
Das [Anlagenmodell](architecture.md#plant-model-and-device-evidence) bildet
diesen Unterschied jetzt ausdrücklich ab und übernimmt die zentrale Zuordnung
zum vorhandenen Gerät.

Zusammengehörige Entitäten bleiben über ihre Standardnamen erkennbar, etwa
Heizkreis 1, Warmwasser 1 und Wärmeerzeuger 1. Die Integration verwendet die
gemeldeten Kreiskennungen und vorhandene konfigurierte Kreisnamen.
Eigene Entitätsnamen in Home Assistant haben weiterhin Vorrang. Mehrere
Gateways bleiben getrennt, auch wenn ihre lokalen Kreisnummern gleich sind.

Diese Entscheidung fügt keine Entitäten hinzu, entfernt keine und ordnet
keine einem anderen Gerät zu. Entitäts-IDs, eindeutige Kennungen, Historie,
Deaktivierungen, eigene Namen, Bereiche, Labels und Geräteverweise bleiben
erhalten. Es gibt keine neue Migration und keine zusätzliche Ansichtsoption.
Vorhandene Kompatibilitätsmigrationen für ältere Vorabstände bleiben bestehen.

Eine Entität kann in Home Assistant weiterhin einen eigenen Bereich erhalten,
wenn ihr Heizkreis zu einem anderen Bereich als das gemeinsame Gerät gehört.
Dieser ausdrücklich gesetzte Entitätsbereich hat Vorrang vor dem Gerätebereich.
Die Integration setzt solche Zuordnungen nicht selbst. Auch Dashboard-Karten
können Entitäten gruppieren, ohne ihre Gerätezuordnung zu verändern.

Übergreifende Anlagenwerte, Meldungen, Urlaubskalender und abgeleitete Werte
bleiben wie bisher dem Gateway zugeordnet. Kontoweite Anfragestatistiken
gehören weiterhin zu keinem einzelnen Gerät. Ein allgemeiner
Wärmeerzeugerwert wird nicht allein deshalb einem bestimmten Erzeuger
zugerechnet, weil gerade nur ein Erzeuger entdeckt wurde.

Eigenständige Geräte des zusätzlichen Cloud-Zugangs und separate
PointT-Wallboxen behalten ihre eigenen Gerätekennungen. Die Entscheidung
führt diese Geräte nicht mit dem Heizungs-Gateway zusammen.

### Falls PointT ein zusätzliches Gerät ausweist

Ein neues Gerät benötigt einen Nachweis im PointT-Gerätemodell, eine stabile
Kennung und eine ausdrückliche Ressourcenzuordnung. Ein Kreisschlüssel,
Modulname oder Ressourcenpfad allein genügt nicht. Weitere Kreise,
Solarfunktionen und andere unterstützte Ressourcengruppen können den Bestand
am selben Gateway erweitern. Das aktuelle Modell leitet daraus keine weiteren
Geräte oder Messwerte ab.

Eine spätere Umsetzung benötigt eine ausdrückliche, umkehrbare Migration
der Zuordnungen. Entitäts-IDs allein zu erhalten reicht nicht aus:
Gerätebezogene Aktionen können nach einer Aufteilung andere Entitäten
ansprechen. Geerbte Bereiche und Labels können sich ebenfalls verändern.
Die Verknüpfung mit einem übergeordneten Gerät überträgt dessen Bereich,
Labels oder Automationsziele nicht automatisch. Eine Migration muss diese
Auswirkungen erklären, Benutzereinstellungen bewahren und vorübergehend
fehlende Geräte berücksichtigen.

Mit dieser Entscheidung ist keine Aufteilung oder Migration umgesetzt worden.
