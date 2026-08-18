# Project specification status

This document tracks the incremental implementation of the externally stored
project specification. The source specification itself is intentionally not
part of this repository. The status reflects implemented and verified code,
not merely planned architecture.

## Accepted deviations

- Energy data is read every 5 minutes instead of every 10 minutes.
- Static values are read during discovery only and not again after 24 hours.

Both deviations are accepted for this project.

## Status by area

| Area | Status | Main remaining work |
|---|---|---|
| Repository, package structure, and CI | implemented | Preview ZIP is reproducible and was imported in isolation; HACS and hassfest also run in the release workflow before publication |
| OAuth/PKCE and token rotation | mostly implemented | Real Bosch and real reauthentication tests |
| Dynamic discovery | implemented | Reconfiguration and capability-based firmware schema checks are available; observe additional real installations |
| Polling and partial failures | mostly implemented | Standard and cloud-friendly profiles are available; gateway jitter remains open |
| Negative capabilities | implemented | Active pauses are reported in aggregate diagnostics |
| State and availability model | implemented | Redacted diagnostics are available |
| Read-only entities | implemented on one K40 | Maturity levels and default activation are implemented; additional installation profiles remain open |
| Faults and notifications | implemented on one K40 | Active fault 6249 is verified; warning, maintenance, critical, historical, and optional component-resource variants need additional real installations |
| Energy | mostly implemented | Direct counters, calculated environmental energy, and reset detection are implemented; long-term testing remains open |
| Diagnostics and support export | implemented | Understandable HTTP and capability success counters are available; observe additional real support cases |
| Reconfiguration and repairs | implemented | Gateway selection, brand changes, rediscovery, polling profiles, rate-limit repair, and firmware-schema repair are available |
| Write service and controls | mostly implemented | Safe user controls are released dynamically and were tested selectively on the reference installation; additional installation profiles remain open |
| Developer CLI | mostly implemented | Anonymized inventory export from Home Assistant diagnostics is available; automated contract-fixture generation can follow if needed |
| Preview gate 0.1.0 | implemented and released | Continue field validation |
| Release gate 1.0 | open | Multiple profiles, Bosch, endurance testing, and additional real write tests |

## Next priorities

1. Additional real Bosch and Buderus installation profiles and anonymized
   inventories.
2. An endurance run after deployment, including token rotation, cloud outages,
   and rate limits.
