"""The private password of an iCom gateway: as typed, without outer spaces, reset.

The setup uses the private password exactly as typed. Only when the gateway's
first answer cannot be decrypted is the same answer tried offline with the
password without spaces at its start and end, then with an empty one (a
gateway whose private password was reset). No further request is sent.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import connection_status as status
from custom_components.bosch_buderus_heating import icom_setup
from custom_components.bosch_buderus_heating.icom_access import (
    IcomAccessError,
    icom_connection_data,
    load_icom_access,
)
from custom_components.bosch_buderus_heating.icom_client import (
    IcomError,
    IcomProblem,
    decode_answer,
    other_private_password,
)
from custom_components.bosch_buderus_heating.icom_crypto import (
    derive_key,
    encrypt,
    reset_key,
)
from custom_components.bosch_buderus_heating.icom_plan import UUID_PATH
from custom_components.bosch_buderus_heating.icom_setup import (
    IcomSetupError,
    prepare_icom_configuration,
)
from custom_components.bosch_buderus_heating.local_setup_history import (
    local_setup_history,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from tests.helpers.options_menu import outcome
from tests.test_connection_status import (
    LAN,
    entry_with,
    icom_runtime,
    outcome_text,
    ready_local,
)
from tests.test_icom_setup import (
    CLOUD,
    GATEWAY_PASSWORD,
    PRIVATE_PASSWORD,
    open_icom,
    proof,
    verify,
)
from tests.test_icom_setup import gateway as gateway
from tests.test_icom_setup import session as session
from tests.test_icom_source import build, source, state, until
from tests.test_icom_source import fast as fast
from tests.test_local_flow import supported as supported
from tests.test_local_setup import registered, starting

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
PACKAGE = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
DOCUMENT = b'{"id":"/gateway/uuid","type":"stringValue","value":"000123"}'


def set_private(gateway, private):
    """The private password the simulated gateway has ("" = reset)."""
    gateway.key = (
        derive_key(GATEWAY_PASSWORD, private)
        if private
        else reset_key(GATEWAY_PASSWORD)
    )


# ------------------------------------------------------------ offline check


def test_the_typed_form_itself_is_never_tried_again():
    answer = encrypt(derive_key(GATEWAY_PASSWORD, "pin"), DOCUMENT)
    assert other_private_password(GATEWAY_PASSWORD, "pin", answer) is None


def test_the_form_without_outer_spaces_is_tried_first():
    answer = encrypt(derive_key(GATEWAY_PASSWORD, "pin"), DOCUMENT)
    found = other_private_password(GATEWAY_PASSWORD, "  pin ", answer)
    assert found is not None
    assert (found.variant, found.private_password) == ("trimmed", "pin")
    assert found.key == derive_key(GATEWAY_PASSWORD, "pin")
    assert found.reply.document["value"] == "000123"
    assert "pin" not in repr(found) and "000123" not in repr(found)


@pytest.mark.parametrize("typed", ["pin", " pin ", "   "])
def test_an_answer_of_a_reset_gateway_is_recognised(typed):
    answer = encrypt(reset_key(GATEWAY_PASSWORD), DOCUMENT)
    found = other_private_password(GATEWAY_PASSWORD, typed, answer)
    assert found is not None
    assert (found.variant, found.private_password) == ("reset", "")


def test_an_answer_that_fits_no_form_gives_nothing():
    answer = encrypt(derive_key(GATEWAY_PASSWORD, "other"), DOCUMENT)
    assert other_private_password(GATEWAY_PASSWORD, " pin ", answer) is None


def test_an_undecryptable_answer_is_kept_with_the_error_but_never_shown():
    answer = encrypt(derive_key(GATEWAY_PASSWORD, "other"), DOCUMENT)
    with pytest.raises(IcomError) as raised:
        decode_answer(derive_key(GATEWAY_PASSWORD, "pin"), answer)
    assert raised.value.problem is IcomProblem.UNDECRYPTABLE
    assert raised.value.answer == answer
    assert str(raised.value) == "undecryptable"
    # No encrypted text at all is no answer of the gateway: nothing is kept.
    with pytest.raises(IcomError) as invalid:
        decode_answer(derive_key(GATEWAY_PASSWORD, "pin"), b"<html>")
    assert invalid.value.problem is IcomProblem.INVALID_RESPONSE
    assert invalid.value.answer is None


# ------------------------------------------------------------------ setup


async def test_a_private_password_with_outer_spaces_is_used_as_typed(gateway, session):
    set_private(gateway, " private-secret ")
    proof_ = await verify(session, gateway, private_password=" private-secret ")
    assert proof_.private_password == " private-secret "
    assert proof_.private_password_variant == "exact"
    assert gateway.requests == [UUID_PATH]


@pytest.mark.parametrize(
    "typed", [" private-secret", "private-secret  ", "  private-secret "]
)
async def test_outer_spaces_the_gateway_does_not_have_are_left_out(
    gateway, session, typed
):
    proof_ = await verify(session, gateway, private_password=typed)
    assert proof_.private_password == PRIVATE_PASSWORD
    assert proof_.private_password_variant == "trimmed"
    # The same answer was decrypted offline: no further request.
    assert gateway.requests == [UUID_PATH]
    assert "private" not in repr(proof_)


def test_the_working_form_is_stored_and_named():
    before, _, target = starting()
    after = prepare_icom_configuration(
        before,
        proof(target, private_password="pin", private_password_variant="trimmed"),
    )
    key = next(key for key in after.connections if key not in before.connections)
    data = after.connections[key].to_data()
    assert data["credentials"]["private_password"] == "pin"
    assert data["icom_access"]["private_password_variant"] == "trimmed"
    access = load_icom_access(data)
    assert access.private_password_variant == "trimmed"
    assert access.key == derive_key("abcdefghijklmnop", "pin")


@pytest.mark.parametrize("typed", ["private-secret", " any ", "   "])
async def test_a_reset_private_password_is_named_and_saves_nothing(
    gateway, session, typed
):
    set_private(gateway, "")
    with pytest.raises(IcomSetupError) as raised:
        await verify(session, gateway, private_password=typed)
    assert str(raised.value) == "icom_no_private_password"
    assert gateway.requests == [UUID_PATH]


@pytest.mark.parametrize("typed", ["wrong", " wrong ", "Private-Secret"])
async def test_a_password_that_fits_no_form_stays_undecryptable(
    gateway, session, typed
):
    with pytest.raises(IcomSetupError) as raised:
        await verify(session, gateway, private_password=typed)
    assert str(raised.value) == "icom_invalid_auth"
    assert gateway.requests == [UUID_PATH]


async def test_the_gateway_password_may_be_typed_with_spaces(gateway, session):
    proof_ = await verify(session, gateway, gateway_password=" abcd efgh - ijkl  mnop ")
    assert proof_.private_password_variant == "exact"
    before, _, target = starting()
    after = prepare_icom_configuration(
        before, proof(target, gateway_password=" abcd efgh ijkl-mnop ")
    )
    key = next(key for key in after.connections if key not in before.connections)
    stored = after.connections[key].to_data()["credentials"]["gateway_password"]
    assert stored == "abcdefghijklmnop"


async def test_the_values_check_reads_on_with_the_form_that_fits(gateway, session):
    gateway.path_status[UUID_PATH] = 404
    proof_ = await verify(
        session, gateway, private_password=" private-secret", cloud_readings=CLOUD
    )
    assert proof_.method == "values"
    assert (proof_.private_password, proof_.private_password_variant) == (
        PRIVATE_PASSWORD,
        "trimmed",
    )
    assert sorted(gateway.requests) == sorted([UUID_PATH, *CLOUD])


async def test_once_an_answer_was_decrypted_no_other_form_is_tried():
    key = derive_key(GATEWAY_PASSWORD, "pin")
    reset_answer = encrypt(reset_key(GATEWAY_PASSWORD), DOCUMENT)
    answers = [decode_answer(key, encrypt(key, DOCUMENT))]

    class Client:
        used: bytes | None = None

        async def get(self, path):
            if answers:
                return answers.pop()
            raise IcomError(IcomProblem.UNDECRYPTABLE, 200, answer=reset_answer)

        def use_key(self, value):
            self.used = value

    client = Client()
    reads = icom_setup._CheckedReads(client, GATEWAY_PASSWORD, "pin")
    await reads.get(UUID_PATH)
    with pytest.raises(IcomError) as raised:
        await reads.get(UUID_PATH)
    assert raised.value.problem is IcomProblem.UNDECRYPTABLE
    assert client.used is None and reads.variant == "exact"


# ----------------------------------------------------------------- dialog


async def test_the_dialog_names_a_reset_gateway_and_notes_the_attempts(
    hass, supported, gateway, caplog
):
    caplog.set_level(logging.DEBUG)
    entry, management, _cloud, _target = registered(hass)
    manager, result = await open_icom(hass, entry)
    form = {
        "host": "127.0.0.1",
        "gateway_password": GATEWAY_PASSWORD,
        "private_password": " private-secret ",
    }
    original = icom_setup.verify_icom_access

    async def on_test_port(*args, **kwargs):
        return await original(*args, **{**kwargs, "port": gateway.port})

    import custom_components.bosch_buderus_heating.icom_flow as dialog

    dialog_verify = dialog.verify_icom_access
    dialog.verify_icom_access = on_test_port
    try:
        set_private(gateway, "")
        reset = await manager.async_configure(result["flow_id"], form)
        assert reset["step_id"] == "icom_access"
        assert reset["errors"] == {"base": "icom_no_private_password"}
        assert len(management.current().connections) == 1
        set_private(gateway, PRIVATE_PASSWORD)
        done = await manager.async_configure(reset["flow_id"], form)
    finally:
        dialog.verify_icom_access = dialog_verify
    assert await outcome(hass, done) == "icom_connected"
    saved = management.current()
    data = next(
        c.to_data() for c in saved.connections.values() if "icom_access" in c.to_data()
    )
    assert data["credentials"]["private_password"] == PRIVATE_PASSWORD
    assert data["icom_access"]["private_password_variant"] == "trimmed"
    attempts = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert [(a["step"], a["result"], a["error"]) for a in attempts] == [
        ("icom_access", "failed", "icom_no_private_password"),
        ("icom_access", "succeeded", None),
    ]
    # Neither password, nor a key, ever reaches the log.
    for secret in (PRIVATE_PASSWORD, "abcd-efgh", "abcdefgh", gateway.key.hex()):
        assert secret not in caplog.text


# ----------------------------------------------------------------- running


async def test_a_running_access_notices_a_reset_gateway_without_using_it(
    hass, gateway, fast
):
    p = build(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        assert source(p).diagnostics()["private_password"] == {
            "variant": "exact",
            "reset_detected": False,
        }
        assert (await source(p).test_connection())[0] == "ok"
        set_private(gateway, "")
        assert (await source(p).test_connection())[0] == "auth_reset"
        await until(
            hass,
            lambda: state(p).condition is LocalCondition.AUTH_REQUIRED,
            seconds=15,
        )
        assert source(p).private_password_reset is True
        assert source(p).diagnostics()["private_password"]["reset_detected"] is True
        # The access keeps its stored key: no value is read with the empty one.
        assert source(p).client._key == derive_key(GATEWAY_PASSWORD, PRIVATE_PASSWORD)
    finally:
        await p.runtime.close()


async def test_a_wrong_stored_password_is_no_reset(hass, gateway, fast):
    p = build(hass, gateway, password="wrong")
    p.runtime.start()
    try:
        await until(
            hass,
            lambda: state(p) and state(p).condition is LocalCondition.AUTH_REQUIRED,
        )
        assert source(p).private_password_reset is False
        assert (await source(p).test_connection())[0] == "auth_required"
    finally:
        await p.runtime.close()


# --------------------------------------------------------- connection test


async def test_the_connection_test_names_a_reset_gateway(hass: HomeAssistant):
    hass.config.language = "en"
    runtime = icom_runtime(hass, ready_local())
    runtime.local_driver.test_connection.return_value = ("auth_reset", 30)
    entry = entry_with(runtime)
    assert (await status.test_connection(hass, entry, LAN))[0] == "auth_icom_reset"
    text = await outcome_text(hass, entry, LAN)
    assert "no private password" in text and "old device app" in text


@pytest.mark.parametrize(
    ("reset", "code"), [(True, "auth_icom_reset"), (False, "auth_icom")]
)
async def test_a_stopped_access_names_a_reset_it_saw(hass: HomeAssistant, reset, code):
    hass.config.language = "en"
    stopped = SimpleNamespace(
        condition=LocalCondition.AUTH_REQUIRED,
        current_observations=[],
        resources=[],
        problem="auth_required",
    )
    runtime = icom_runtime(hass, stopped)
    runtime.local_driver = SimpleNamespace(
        test_connection=AsyncMock(return_value=("ok", 1)),
        private_password_reset=reset,
    )
    entry = entry_with(runtime)
    assert (await status.test_connection(hass, entry, LAN))[0] == code
    runtime.local_driver.test_connection.assert_not_awaited()


# ------------------------------------------------------------ stored access


def test_a_stored_access_without_a_named_form_was_used_as_typed():
    data = icom_connection_data(
        host="192.168.1.20",
        gateway_password=GATEWAY_PASSWORD,
        private_password="pin",
        gateway_id="000123",
        verified_at=1.0,
    )
    del data["icom_access"]["private_password_variant"]
    assert load_icom_access(data).private_password_variant == "exact"
    data["icom_access"]["private_password_variant"] = "reset"
    with pytest.raises(IcomAccessError, match="invalid_icom_access"):
        load_icom_access(data)
    with pytest.raises(IcomAccessError, match="invalid_icom_access"):
        icom_connection_data(
            host="192.168.1.20",
            gateway_password=GATEWAY_PASSWORD,
            private_password="pin",
            gateway_id="000123",
            verified_at=1.0,
            private_password_variant="reset",
        )


# ------------------------------------------------------------------ texts


def _options(language):
    path = PACKAGE / (
        "strings.json" if language is None else f"translations/{language}.json"
    )
    return json.loads(path.read_text("utf-8"))["options"]


LANGUAGES = sorted(path.stem for path in (PACKAGE / "translations").glob("*.json"))


def test_the_english_texts_name_case_length_and_the_reset():
    options = _options(None)
    wrong = options["error"]["icom_invalid_auth"]
    assert "upper and lower case" in wrong and "4 to 16 characters" in wrong
    assert "after a reset" in wrong and "same Wi-Fi as the gateway" in wrong
    for text in (
        options["error"]["icom_no_private_password"],
        options["step"]["connection_test_auth_icom_reset"]["description"],
    ):
        assert "no private password stored" in text
        assert "old device app" in text and "same Wi-Fi as the gateway" in text
    assert (
        "dashes and spaces are ignored"
        in (options["step"]["icom_access"]["data_description"]["gateway_password"])
    )
    assert _options("en") == options


# The note on upper and lower case in each language's decryption error.
CASE_NOTES = {
    "de": "Groß- und Kleinschreibung zählt",
    "cs": "záleží na velkých a malých písmenech",
    "sk": "záleží na veľkých a malých písmenách",
    "bg": "главните и малките букви имат значение",
    "da": "der skelnes mellem store og små bogstaver",
    "el": "τα κεφαλαία και τα πεζά έχουν σημασία",
    "es": "se distinguen mayúsculas y minúsculas",
    "fi": "isot ja pienet kirjaimet ovat merkitseviä",
    "fr": "majuscules et minuscules comptent",
    "ga": "tá difríocht idir cás uachtair agus íochtair",
    "hr": "velika i mala slova su bitna",
    "it": "maiuscole e minuscole contano",
    "nb": "store og små bokstaver teller",
    "nl": "hoofdletters en kleine letters tellen",
    "pl": "wielkość liter ma znaczenie",
    "pt": "maiúsculas e minúsculas contam",
    "sl": "velike in male črke so pomembne",
    "sv": "stora och små bokstäver räknas",
}


@pytest.mark.parametrize("language", [x for x in LANGUAGES if x != "en"])
def test_every_language_translates_the_new_texts(language):
    english, options = _options("en"), _options(language)
    for path in (
        ("error", "icom_no_private_password"),
        ("error", "icom_invalid_auth"),
        ("step", "connection_test_auth_icom_reset", "description"),
        ("step", "connection_test_auth_icom", "description"),
        ("step", "icom_access", "data_description", "gateway_password"),
    ):
        text, original = options, english
        for part in path:
            text, original = text[part], original[part]
        assert text and text != original, (language, path)
    # The length of the private password and the case are named everywhere.
    assert "16" in options["error"]["icom_invalid_auth"]
    assert CASE_NOTES[language] in options["error"]["icom_invalid_auth"]
    assert (
        options["step"]["connection_test_auth_icom_reset"]["title"]
        == options["step"]["connection_test_auth_icom"]["title"]
    )
