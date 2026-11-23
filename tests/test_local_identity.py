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


UNREPORTED = SystemModule(
    "8", "65519", "1.3.0+1", None, "Burner module 239", None, None
)


@pytest.mark.parametrize("cloud_serial", [None, "", "00099999999999999999999999"])
def test_a_local_module_without_serial_number_is_left_out_of_the_comparison(
    cloud_serial,
):
    """#139: the other modules still prove the identity."""
    cloud = cloud_system()
    row = {"Tok": "8", "Id": "65519", "ModuleHwIdentStr": "Burner module 239"}
    if cloud_serial is not None:
        row["ModuleSerialNumber"] = cloud_serial
    cloud["values"].append(row)
    compare_systems(replace(SYSTEM, modules=(MODULE, UNREPORTED)), cloud, "000123")


def test_a_system_with_only_unreported_modules_proves_no_identity():
    cloud = cloud_system()
    cloud["values"] = [
        {"Tok": "8", "Id": "65519", "ModuleHwIdentStr": "Burner module 239"}
    ]
    with pytest.raises(ConfigurationError, match="identity_contract_required"):
        compare_systems(replace(SYSTEM, modules=(UNREPORTED,)), cloud, "000123")


GATEWAY_ROW = SystemModule("72", "17", "15.00.01", "4711", "K40RF", None, None)
INDOOR = SystemModule("8", "8", "1.0", "0009876", "XCU_THH", None, None)
TOLERANT_LOCAL = replace(SYSTEM, modules=(MODULE, GATEWAY_ROW, INDOOR))


def tolerant_cloud(**gateway_row):
    """A cloud module list in other forms than the local one."""
    cloud = cloud_system()
    cloud["values"] += [
        # The gateway's serial number as a whole number.
        {
            "Tok": "72",
            "Id": "17",
            "ModuleSerialNumber": 4711,
            "ModuleHwIdentStr": "K40RF",
        }
        | gateway_row,
        # Another serial number of a module that is not the gateway's.
        {
            "Tok": "8",
            "Id": "8",
            "ModuleSerialNumber": "0001234",
            "ModuleHwIdentStr": "XCU_THH",
        },
        # Two rows with the same place and type say nothing.
        {"Tok": "9", "Id": "9", "ModuleSerialNumber": "1", "ModuleHwIdentStr": "a"},
        {"Tok": "9", "Id": "9", "ModuleSerialNumber": "2", "ModuleHwIdentStr": "b"},
        # Rows without place or type, and no row at all.
        {"Id": "5", "ModuleSerialNumber": "3"},
        {"Tok": None, "Id": "6"},
        "not a module",
        None,
    ]
    return cloud


def test_unusable_rows_are_left_out_and_one_match_is_enough():
    compare_systems(TOLERANT_LOCAL, tolerant_cloud(), "000123")


def test_a_serial_number_is_compared_by_its_digits():
    cloud = cloud_system()
    cloud["values"][0]["ModuleSerialNumber"] = " " + MODULE.serial_number + " "
    compare_systems(SYSTEM, cloud, "000123")
    number = replace(MODULE, serial_number=int(MODULE.serial_number))
    compare_systems(replace(SYSTEM, modules=(number,)), cloud_system(), "000123")


@pytest.mark.parametrize(
    "gateway_row",
    [{"ModuleSerialNumber": 4712}, {"ModuleHwIdentStr": "MX300"}],
)
def test_another_gateway_row_is_another_gateway_whatever_else_matches(gateway_row):
    with pytest.raises(ConfigurationError, match="local_identity_conflict"):
        compare_systems(TOLERANT_LOCAL, tolerant_cloud(**gateway_row), "000123")


def test_a_place_and_type_reported_twice_compare_nothing():
    # Which of two rows with the same key is the local one cannot be told:
    # neither confirms nor contradicts; another module still matches.
    cloud = cloud_system()
    cloud["values"] += [
        {
            "Tok": "72",
            "Id": "17",
            "ModuleSerialNumber": "4711",
            "ModuleHwIdentStr": "K40RF",
        },
        {
            "Tok": "72",
            "Id": "17",
            "ModuleSerialNumber": "4712",
            "ModuleHwIdentStr": "K40RF",
        },
    ]
    compare_systems(TOLERANT_LOCAL, cloud, "000123")
    twice = replace(
        SYSTEM, modules=(MODULE, GATEWAY_ROW, replace(GATEWAY_ROW, serial_number="1"))
    )
    compare_systems(twice, tolerant_cloud(ModuleSerialNumber=4712), "000123")


@pytest.mark.parametrize(
    ("local", "cloud"),
    [
        (-1, -1),
        ("-1", "-1"),
        (-1, "-1"),
        (0, "0"),
        ("0000", 0),
        (1, "1"),
        ("123", 123),
        ("0001", 1),
        ("000123", "000123"),
        ("N/A", "N/A"),
        ("n/a", "NA"),
        ("-", "-"),
        ("--", "--"),
        ("", ""),
        ("unknown", "Unknown"),
        ("NONE", "none"),
        ("not available", "Not Available"),
        ("1111", 1111),
        ("FFFFFFFF", "ffffffff"),
        ("-12345678", -12345678),
    ],
)
def test_a_placeholder_serial_number_identifies_no_module(local, cloud):
    # The same placeholder and hardware name on both sides prove nothing:
    # with no other module, the check asks for evidence instead of accepting.
    module = replace(MODULE, serial_number=local)
    system = cloud_system()
    system["values"][0]["ModuleSerialNumber"] = cloud
    with pytest.raises(ConfigurationError, match="identity_contract_required"):
        compare_systems(replace(SYSTEM, modules=(module,)), system, "000123")


@pytest.mark.parametrize("serial", ["4711", 4711, "A1B2", "0004711"])
def test_four_letters_or_digits_identify_a_module(serial):
    module = replace(MODULE, serial_number=serial)
    system = cloud_system()
    system["values"][0]["ModuleSerialNumber"] = serial
    compare_systems(replace(SYSTEM, modules=(module,)), system, "000123")


@pytest.mark.parametrize("placeholder", [-1, "N/A", "unknown"])
def test_a_placeholder_at_the_gateway_place_neither_proves_nor_contradicts(
    placeholder,
):
    # Another module still proves the system; without it nothing does.
    cloud = tolerant_cloud(ModuleSerialNumber=placeholder)
    compare_systems(TOLERANT_LOCAL, cloud, "000123")
    only_gateway = replace(SYSTEM, modules=(GATEWAY_ROW,))
    with pytest.raises(ConfigurationError, match="identity_contract_required"):
        compare_systems(only_gateway, cloud, "000123")


def test_only_contradicting_modules_are_another_system():
    cloud = cloud_system()
    cloud["values"][0]["ModuleSerialNumber"] = "00098765432109876543210987"
    with pytest.raises(ConfigurationError, match="local_identity_conflict"):
        compare_systems(SYSTEM, cloud, "000123")


@pytest.mark.parametrize(
    "change", [{"gatewayId": "000124"}, {"status": "in_progress"}, {"id": "/x"}]
)
def test_the_gateway_id_and_the_list_itself_stay_strict(change):
    with pytest.raises(ConfigurationError, match="local_identity_conflict"):
        compare_systems(TOLERANT_LOCAL, tolerant_cloud() | change, "000123")


def test_a_completed_status_in_another_case_is_completed():
    compare_systems(SYSTEM, cloud_system() | {"status": " Completed "}, "000123")


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


@pytest.mark.parametrize("gateway_serial", [4711, 4712])
async def test_the_cloud_list_in_other_forms_proves_the_same_gateway(
    hass, monkeypatch, gateway_serial
):
    # The actual cloud read: one match is enough; the gateway's own row
    # must still agree.
    backend = IdentityBackend()
    backend.system = tolerant_cloud(ModuleSerialNumber=gateway_serial)
    async with running(hass, backend, configure=matching_local) as (_, _, owner, _):
        snapshot, target, proof = await local_proof(owner, monkeypatch)
        proof = replace(proof, system=TOLERANT_LOCAL)
        policy = LocalIdentityPolicy(hass)
        if gateway_serial == 4711:
            contract = await policy.verify(snapshot, target, proof)
            assert contract.contract_id == CONTRACT
        else:
            with pytest.raises(ConfigurationError, match="local_identity_conflict"):
                await policy.verify(snapshot, target, proof)
        assert owner.connections.management.current() == snapshot


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
