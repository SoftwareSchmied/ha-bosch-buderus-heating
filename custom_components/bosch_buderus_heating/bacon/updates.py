"""Merge reported messages without turning unrelated cached fields fresh."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import BaconError, Device, Shadow, shadow


def sensor_sample(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the sensor envelope and choose its newest timestamped row."""
    items = payload.get("items")
    if not isinstance(items, list) or (
        payload.get("deviceType") is not None
        and not isinstance(payload["deviceType"], str)
    ):
        raise BaconError("invalid_sensor")
    latest: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            raise BaconError("invalid_sensor")
        stamp = item.get("timestamp")
        if type(stamp) is not int or stamp < 0:
            raise BaconError("invalid_sensor")
        row = {key: value for key, value in item.items() if value is not None}
        if latest is None or stamp > latest["timestamp"]:
            latest = row
        elif stamp == latest["timestamp"] and row != latest:
            raise BaconError("conflicting_sensor_timestamp")
    return latest


def expire_values(
    device: Device, now: float, state_max_age: float, sensor_max_age: float
) -> None:
    """Expire only fields whose own last observation has aged out."""
    for key, received in tuple(device.sensor_fields_received_at.items()):
        if now - received > sensor_max_age:
            device.sensor.pop(key, None)
            device.sensor_fields_received_at.pop(key)
    if device.state is not None:
        reported = dict(device.state.reported)
        for key, received in tuple(device.state_fields_received_at.items()):
            if now - received > state_max_age:
                reported.pop(key, None)
                device.state_fields_received_at.pop(key)
        device.state = replace(device.state, reported=reported)


def ingest_sensor(
    device: Device, payload: dict[str, Any], generation: int, now: float
) -> bool:
    """Return whether the envelope carried a new, previously unseen sample."""
    sample = sensor_sample(payload)
    if sample is None or sample["timestamp"] <= device.sensor_timestamp:
        return False
    if device.sensor_generation != generation:
        device.sensor = {}
        device.sensor_fields_received_at.clear()
    device.sensor_timestamp = sample["timestamp"]
    fields = {key: value for key, value in sample.items() if key != "timestamp"}
    if not fields:
        return True
    device.sensor.update(fields)
    device.sensor["timestamp"] = sample["timestamp"]
    device.sensor_fields_received_at.update(dict.fromkeys(fields, now))
    device.sensor_received_at = now
    device.sensor_generation = generation
    return True


def _merge(
    original: dict[str, Any], patch: dict[str, Any], *, delete_null: bool = False
) -> dict[str, Any]:
    result = dict(original)
    for key, value in patch.items():
        if value is None:
            if delete_null:
                result.pop(key, None)
        elif isinstance(value, dict):
            previous = result.get(key)
            result[key] = _merge(
                previous if isinstance(previous, dict) else {},
                value,
                delete_null=delete_null,
            )
        else:
            result[key] = value
    return result


def ingest_state(
    device: Device,
    payload: dict[str, Any],
    generation: int,
    now: float,
    *,
    full: bool,
) -> Shadow | None:
    """A get response replaces the snapshot; an update supplements its fields."""
    incoming = shadow(payload)
    previous = device.state
    if previous and incoming.version < previous.version:
        return None
    reported = {
        key: value for key, value in incoming.reported.items() if value is not None
    }
    same_generation = device.state_generation == generation
    if previous and incoming.version == previous.version:
        # A get may refresh unchanged fields; replayed updates must not do so.
        for key in previous.reported.keys() & reported.keys():
            if (
                device.state_fields_version.get(key) == incoming.version
                and previous.reported[key] != reported[key]
            ):
                raise BaconError("conflicting_version")
        if (
            not full
            and all(
                device.state_fields_version.get(key) == incoming.version
                for key in reported
            )
            and incoming.desired == previous.desired
        ):
            return None
    if not same_generation:
        device.state_fields_received_at.clear()
        device.state_fields_version.clear()
    if not full and incoming.version <= device.state_snapshot_version:
        return None
    if not full:
        # Expired observations retain their versions so a replay cannot revive them.
        reported = {
            key: value
            for key, value in reported.items()
            if device.state_fields_version.get(key, -1) < incoming.version
        }
    old_reported = previous.reported if previous and same_generation else {}
    old_desired = previous.desired if previous and same_generation else {}
    if full and "reported" in payload["state"]:
        device.state_snapshot_version = incoming.version
        merged_reported = reported
        merged_desired = incoming.desired
        device.state_fields_received_at.clear()
        device.state_fields_version.clear()
    else:
        merged_reported = _merge(old_reported, reported)
        merged_desired = _merge(old_desired, incoming.desired, delete_null=True)
    device.state = replace(incoming, reported=merged_reported, desired=merged_desired)
    # Desired-only acknowledgements neither confirm nor refresh reported fields.
    if reported:
        fresh = {
            key: now
            for key in reported
            if full or device.state_fields_version.get(key, -1) < incoming.version
        }
        device.state_fields_received_at.update(fresh)
        device.state_fields_version.update(dict.fromkeys(fresh, incoming.version))
        if fresh:
            device.state_generation = generation
            device.state_received_at = now
            device.state_reports += 1
    return device.state if full and "reported" in payload["state"] else None
