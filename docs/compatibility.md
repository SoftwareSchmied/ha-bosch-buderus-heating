# API, firmware and specification compatibility

This page records which specification the local access is built on, which
real systems and firmware versions were checked, and how the integration
reacts when the cloud or a gateway reports something different from what it
knows. It is maintained together with
[resource contracts](resource-contracts.md) and the
[specification status](specification-status.md).

## Home Assistant

The integration requires Home Assistant 2026.5.0 or newer: from that release a
setup dialog can continue directly with the local access of the new entry. The
automated checks run the full suite on Home Assistant 2026.5.4, 2026.9.2 and
the current release.

## Specification

| Source | Version | Used for |
| --- | --- | --- |
| Official local API documentation, `openapi/k-40-rf.yaml` | commit `78e36b3` of 2026-09-11, `info.version` `v15.00.01` (pinned with its SHA-256 in `local_api/_data.py`) | paths, response schemas and parameters of the local access |
| Official guide for creating and revoking the local token | same commit | sign-in of the local access; checked by hand, see below |

The catalog is generated with `scripts/update_local_api_catalog.py`. A newer
specification is never loaded at runtime: the script refuses a source with a
different checksum until the new version has been reviewed and the pin is
changed deliberately. As of 2026-11-05 no newer version has been published.

### Reviewing a new specification version

1. Compare it with the pin. This reads the candidate with the same rules as the
   catalog and changes nothing:

   ```bash
   python -m scripts.compare_local_api_spec path/to/k-40-rf.yaml
   ```

   It compares the read contract: the successful JSON response, parameters,
   the examples, the sign-in scheme and the server. Examples are compared per
   path, so renaming an example does not hide a changed unit or value. Error
   responses and descriptions are not compared. Each difference is sorted into
   one of three classes and names the affected read templates, marked `shown`
   (a paired entity) or `read` (polling plan):

   | Class | Examples | Meaning |
   | --- | --- | --- |
   | `compatible` | new path, new optional field, new example, new allowed parameter value, a response value no longer documented, a parameter that becomes optional | Known contracts stay valid. A new path needs a polling class in `local_polling/_data.py` before it can be read, and a reviewed entity contract before anything is shown. |
   | `adapt` | changed type, format, unit, limit, marker or allowed value (also when stated only in an example), a field no longer guaranteed, an optional field removed, a new required or a removed parameter, renamed path parameters, a deprecated path, a changed sign-in scheme or server, an example that newly contradicts its meaning, anything the extraction does not know (reported with its place) | Review the adapters and tests before moving the pin. |
   | `breaking` | a known path, schema or guaranteed field removed | Plan the removal; entities based on it lose their local value. |

   A renamed schema shows up as removed and added; a reordered `allOf` list as
   changed entries. Both err on the side of a review.

   Exit codes: `0` nothing or only compatible changes, `3` adaptation needed,
   `4` something breaks; a crash exits with `1`. `--json` gives the same
   report in machine-readable form.
2. Review the changes and adapt the narrow, hand-written adapters and their
   tests. Code is never generated from the specification. A new field or path
   never becomes an entity, a write command or a mandatory read on its own.
3. Check the sign-in guide against the checklist below.
4. Move the pin with `scripts/update_local_api_catalog.py` (new commit and
   checksum) and run the full test suite, including
   `tests/test_spec_examples.py`.

Nothing is reported to the manufacturer or published automatically; open
questions about a contract are first documented here.

### Sign-in checklist

The resource specification describes only the 261 read operations. Token
creation and revocation are described in the separate guide; for a new
version, check by hand:

- the address and method of the token request and of the revocation;
- the confirmation on the device (button or presence confirmation) and its
  time limit;
- the format and lifetime of the token and how a revoked token is answered;
- whether the token remains read-only.

Any change here is a change of `local_auth.py`, `local_enrollment.py` and their
tests, never a silent update.

### Known deviations of the official examples

The examples are checked against their meaning, not only against the schema
(`tests/test_spec_examples.py`):

| Example | Deviation | Handling |
| --- | --- | --- |
| return temperatures (`/heatSources/returnTemperature`, `…/returnFlowTemp`) | `-3276.8`, a tenth of the declared marker `-32768` | treated as an error marker, never as a temperature |
| ventilation temperatures | `-32768` without a declared marker | treated as an error marker (also `32767` and the tenth forms); the disabled local-only sensors show no value |
| `/ventilation/…/supplyFanRotation` | misspelled unit `rpm"` | kept on the reading, never used as a measurement |
| `/devices/{deviceId}/type` | value outside its own allowed values | text shown as reported |

## Checked systems

| System | Gateway firmware | Access | Evidence |
| --- | --- | --- | --- |
| K40 heat pump gateway (`K40RF_v1`) | 15.00.01 | cloud and local | anonymized cloud capture `tests/fixtures/contracts/captures/k40-cloud-2026-11-05.json`, practical checks on my own system |

### Local access: checked on my system and not verifiable there

Checked in practice on my own system (K40 gateway, firmware 15.00.01):

- discovery on the network, adding the local access with an existing token,
  identity comparison with the cloud;
- regular reading of about 180 local resources, including the signal list, with
  the cloud running and with the cloud access switched off at runtime;
- local values for paired entities, local-only sensors, the local error list as
  a second fault source (with no active fault), and diagnostics without
  credentials or network data.

Not verifiable on my system, so covered only by tests:

- further heating circuits, solar, ventilation and room zones over the local
  access, radio devices and their zone relations, module or gateway exchange;
- signing in with the details printed on the device (it creates a new token on
  the gateway), a real certificate change, removing and adding the local access
  again;
- a start that needs a new cloud sign-in, real network and certificate
  failures, long-term stability, other brands and firmware versions;
- the same fault reported by both sources (no fault occurred so far).

The local access covers only the heating and ventilation system of the gateway.
Air conditioners, wallboxes and other devices of the same account have no local
access through it. Only the K40 gateway has a documented local interface, and
the integration uses it for reading only; all other device families are
connected through the cloud. Depending on the model, the local energy readings
may not include the consumption parts (compressor, auxiliary heater,
electricity); these then come only from the cloud, and values calculated from
them show nothing without it.

Further systems are listed in the [device families](device-support-plan.md)
with what they show and operate. A documented specification version, a firmware
version and a checked device are separate facts; one does not imply the others.

## How changes are handled

| Change | Cloud | Local access |
| --- | --- | --- |
| Additional unknown field | Ignored; the known value is used. Weekly programs with unknown fields stay read-only. | Allowed where the schema permits it; otherwise the reading is invalid and the cloud value is shown. |
| New value of a known enum | The sensor shows "unknown", logs once without the value and counts it in diagnostics. | The reading is invalid; the cloud value is shown. |
| Different type (for example text instead of a number) | The entity shows no value; logged once without the value, counted as `changed_contracts_detected`. | The reading is invalid; the cloud value is shown. |
| Different unit of the same quantity (for example °F instead of °C, kPa instead of bar) | Value and unit come from the same reading; Home Assistant converts them to the unit shown. Adjustable values take their unit from the reading, so value and limits stay consistent. | Not comparable; the cloud value is shown. |
| Unit of another quantity or no unit | The sensor shows no value. | The reading is invalid; the cloud value is shown. |
| Declared error marker | No value. | Never supplied. |
| Changed shape of an important capability | A repair issue names the number of conflicts; everything else keeps working. | — |
| Writing | Only for controls with a reviewed contract (range, step, allowed values). A missing range, another quantity or text instead of a number removes the write. | Read only. |
| Firmware update while running | The firmware versions are read every 15 minutes; a change leads to one discovery after the next working cycle. Rate limits and the protection pause postpone it; a failed attempt is repeated after 5, 15 and 60 minutes and then every six hours, until one day after the first failure. New parts become entities; parts missing afterwards are searched again. | The polling plan is renewed; below the minimum firmware 15.00.01 the local access pauses with the cause "firmware update required". After updating the gateway, reload the integration to resume it. |
| Firmware different from the last start | Noted in diagnostics; the start-up discovery checks the capabilities anyway. | — |
| Parts missing after a start | Searched again on a schedule ([diagnostics](diagnostics.md)). | — |

The discovery after a firmware change reads the whole plant again, like a
start. A part that does not answer in that moment is unavailable until the
search for missing parts finds it again.

Diagnostics show these cases under `firmware_changes`, `discovery_recovery`,
`changed_contracts_detected` and the local reasons for using the cloud value.

## Regression captures

An anonymized capture is a real discovery result with its firmware. Gateway
IDs, serial numbers, UUIDs, module identifiers, network values and
user-defined names are replaced; measurements stay. The entities built from
each capture are pinned in a matching `.expected.json`. A code or firmware
change that alters them makes `tests/test_captures.py` fail and needs a
review before the expectation is regenerated.
