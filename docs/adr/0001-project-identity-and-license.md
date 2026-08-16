# ADR 0001: Project identity and license

- Status: Accepted, amended by ADR 0005
- Date: 2026-08-16

## Decision

Use repository `ha-bosch-buderus-heating`, Home Assistant domain
`bosch_buderus_heating`, display name “Bosch/Buderus Heating”, internal PointT
module `custom_components.bosch_buderus_heating.pointt`, and the term “PointT
Cloud API” in technical documentation. License original work under MIT.

The project is explicitly unofficial and uses vendor marks only to describe
compatibility. Foreign code requires provenance and preserved license notices.

## Consequences

These identifiers are stable public contracts. Changes require migrations.
The internal PointT client and integration share one repository and release.
