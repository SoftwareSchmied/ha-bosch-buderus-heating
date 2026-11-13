"""Fresh cross-source evidence and later offline reuse of one retained target."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.local_api.models import (
    SystemInfo,
    SystemModule,
)
from custom_components.bosch_buderus_heating.local_identity import (
    CONTRACT,
    LocalIdentityPolicy,
    compare_systems,
    retained_contract,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
from custom_components.bosch_buderus_heating.local_setup import (
    begin_local_setup,
    prepare_local_configuration,
)
from tests.test_local_setup import verified
from tests.test_managed_pointt import Backend, eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

MODULE = SystemModule(
    "controller", "01", "1.0", "00012345678901234567890123", "hardware-a", None, None
)
SYSTEM = SystemInfo("completed", "", "000123", (MODULE,))


def cloud_system():
    return {
        "id": "/system/info",
        "type": "systeminfo",
        "status": "completed",
        "gatewayId": "000123",
        "values": [
            {
                "Tok": MODULE.token,
                "Id": MODULE.id,
                "ModuleSerialNumber": MODULE.serial_number,
                "ModuleHwIdentStr": MODULE.hardware,
            }
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "gateway",
        "path",
        "type",
        "status",
        "empty",
        "duplicate",
        "serial",
        "number",
        "rounded",
        "hardware",
        "no_match",
        "bad_key",
        "too_many",
        "bad_row",
    ],
)
def test_ambiguous_or_conflicting_systems_never_prove_identity(mutation):
    cloud = cloud_system()
    if mutation in ("gateway", "path", "type", "status"):
        cloud[
            {"gateway": "gatewayId", "path": "id", "type": "type", "status": "status"}[
                mutation
            ]
        ] = "wrong"
    elif mutation == "empty":
        cloud["values"] = []
    elif mutation == "duplicate":
        cloud["values"] *= 2
    elif mutation == "too_many":
        cloud["values"] *= 513
    elif mutation == "bad_row":
        cloud["values"] = ["not a module"]
    else:
        field, value = {
            "serial": ("ModuleSerialNumber", "different"),
            "number": ("ModuleSerialNumber", 123),
            "rounded": ("ModuleSerialNumber", 1.2345678901234568e25),
            "hardware": ("ModuleHwIdentStr", "other"),
            "no_match": ("Id", "02"),
            "bad_key": ("Tok", None),
        }[mutation]
        cloud["values"][0][field] = value
    with pytest.raises(ConfigurationError):
        compare_systems(SYSTEM, cloud, "000123")


def test_extra_metadata_modules_and_changed_firmware_do_not_change_identity():
    cloud = cloud_system()
    cloud["values"].append({"Tok": "extra", "Id": "02", "ModuleSerialNumber": 1.2e21})
    cloud["values"][0]["ProductName"] = "Private name"
    cloud["values"][0]["Ver"] = "new firmware"
    compare_systems(SYSTEM, cloud, "000123")
    with pytest.raises(ConfigurationError):
        compare_systems(replace(SYSTEM, modules=(MODULE, MODULE)), cloud, "000123")


class IdentityBackend(Backend):
    def __init__(self):
        super().__init__()
        self.metadata_calls = 0
        self.alias = None
        self.system = cloud_system()

    def resource(self, gateway, path):
        return (
            self.system if path == "/system/info" else super().resource(gateway, path)
        )

    async def metadata(self, request):
        self.metadata_calls += 1
        await self.wait("identity")
        if status := self.status.get("identity"):
            return web.Response(status=status, headers={"Retry-After": "900"})
        return web.json_response(
            {
                "deviceId": request.match_info["gateway"],
                "id": self.alias or request.match_info["gateway"],
            }
        )

    @property
    def routes(self):
        return [*super().routes, ("GET", "/gateways/{gateway}", self.metadata)]


async def local_proof(owner, monkeypatch):
    await eventually(lambda: source_for(owner, "000123"))
    snapshot = owner.connections.management.current()
    target = snapshot.bindings["local"].target_id
    await eventually(
        lambda: all(
            owner.connections.source_context(k) is not None
            for k, b in snapshot.bindings.items()
            if b.target_id == target and b.adapter == "pointt"
        )
    )
    edit = begin_local_setup(
        snapshot,
        target,
        LocalEndpoint("gateway.example.invalid"),
        connection_id="local",
    )
    proof = await verified(monkeypatch, snapshot, edit)
    return snapshot, target, replace(proof, system=SYSTEM)


def matching_local(data, options):
    old = data["bindings"]["local"]["target_id"]
    target = next(
        k
        for k, v in data["targets"].items()
        if any(
            i["namespace"] == "pointt_gateway" and i["value"] == "000123"
            for i in v["identities"]
        )
    )
    if old != target:
        identity = next(
            i
            for i in data["targets"][old]["identities"]
            if i["namespace"] == "local_system_gateway"
        )
        data["targets"][old]["identities"].remove(identity)
        data["targets"][target]["identities"].append(identity)
        data["bindings"]["local"]["target_id"] = target
        options["targets"][old]["read_order"].remove("local")
        options["targets"][target]["read_order"].append("local")


async def test_actual_cloud_reads_compare_before_tolerant_parsing_then_reuse_offline(
    hass, monkeypatch
):
    backend = IdentityBackend()
    async with running(hass, backend, configure=matching_local) as (
        _entry,
        cloud,
        owner,
        _,
    ):
        snapshot, target, proof = await local_proof(owner, monkeypatch)
        policy = LocalIdentityPolicy(hass)
        assert retained_contract(snapshot, target, "000123") is None
        contract = await policy.verify(snapshot, target, proof)
        assert contract.contract_id == CONTRACT and backend.metadata_calls == 1
        after = prepare_local_configuration(
            proof.edit,
            proof,
            (snapshot,),
            verified_namespace_pairs=contract.verified_pairs(
                snapshot, target, "000123"
            ),
            contract_id=contract.contract_id,
        )
        assert retained_contract(after, target, "000123") == contract
        # A retained target keeps its proof when the cloud connection is removed.
        data, options = after.to_data(), after.to_options()
        removed = {
            k for k, v in data["bindings"].items() if v["connection_id"] == cloud
        }
        del data["connections"][cloud]
        del options["connections"][cloud]
        for key in removed:
            del data["bindings"][key]
            del options["bindings"][key]
        for settings in options["targets"].values():
            settings["read_order"] = [
                key for key in settings["read_order"] if key not in removed
            ]
        offline = load_configuration(snapshot.owner_id, data, options, minor_version=2)
        offline_edit = begin_local_setup(
            offline,
            target,
            LocalEndpoint("gateway.example.invalid"),
            connection_id="local",
        )
        offline_proof = await verified(monkeypatch, offline, offline_edit)
        assert await policy.verify(offline, target, offline_proof) == contract
        assert backend.metadata_calls == 1
        assert retained_contract(offline, target, "different") is None


async def test_conflicting_gateway_alias_is_rejected_before_acceptance(
    hass, monkeypatch
):
    backend = IdentityBackend()
    backend.alias = "different"
    async with running(hass, backend, configure=matching_local) as (_, _, owner, _):
        snapshot, target, proof = await local_proof(owner, monkeypatch)
        with pytest.raises(ValueError, match="local_identity_conflict"):
            await LocalIdentityPolicy(hass).verify(snapshot, target, proof)
        assert owner.connections.management.current() == snapshot


async def test_disabled_source_during_comparison_cannot_supply_a_contract(
    hass, monkeypatch
):
    backend = IdentityBackend()
    async with running(hass, backend, configure=matching_local) as (_, cloud, owner, _):
        snapshot, target, proof = await local_proof(owner, monkeypatch)
        backend.gates["identity"] = asyncio.Event()
        task = asyncio.create_task(
            LocalIdentityPolicy(hass).verify(snapshot, target, proof)
        )
        await eventually(lambda: backend.metadata_calls == 1)
        owner.connections.management.set_enabled(
            owner.connections.management.edit(cloud), False
        )
        backend.gates["identity"].set()
        with pytest.raises((ConfigurationError, asyncio.CancelledError)):
            await task


async def test_cloud_rate_limit_is_reported_without_saving_a_relationship(
    hass, monkeypatch
):
    backend = IdentityBackend()
    async with running(hass, backend, configure=matching_local) as (_, _, owner, _):
        snapshot, target, proof = await local_proof(owner, monkeypatch)
        backend.status["identity"] = 429
        with pytest.raises(ConfigurationError, match="local_cloud_rate_limited"):
            await LocalIdentityPolicy(hass).verify(snapshot, target, proof)
        assert (
            backend.metadata_calls == 1
            and owner.connections.management.current() == snapshot
        )


async def test_contract_name_on_an_unverified_identity_is_insufficient(
    hass, monkeypatch
):
    backend = IdentityBackend()
    async with running(hass, backend, configure=matching_local) as (_, _, owner, _):
        snapshot, target, _ = await local_proof(owner, monkeypatch)
        data = deepcopy(snapshot.to_data())
        local = next(
            i
            for i in data["targets"][target]["identities"]
            if i["namespace"] == "local_system_gateway"
        )
        local["comparison_contract"] = CONTRACT
        altered = load_configuration(snapshot.owner_id, data, snapshot.to_options())
        assert retained_contract(altered, target, "000123") is None
