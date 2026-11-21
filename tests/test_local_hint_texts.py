"""The hints for the local gateway setup and the token dialogs are in every language."""

import json
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
FILES = [PACKAGE / "strings.json", *sorted((PACKAGE / "translations").glob("*.json"))]


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_the_unlock_error_names_the_way_out_for_another_network(path):
    data = _load(path)
    title = data["config"]["step"]["local_token"]["title"]
    for part in ("config", "options"):
        # The second cause of the 412 answer and the existing token as the way out.
        assert title in data[part]["error"]["local_unlock_required"], (path, part)


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_the_token_dialogs_do_not_promise_the_revocation_of_older_tokens(path):
    steps = _load(path)["options"]["step"]
    for step in ("local_tokens", "local_tokens_remove"):
        text = steps[step]["description"]
        # Older tokens are no longer described as usually remaining; the
        # sentence that replaces it and the one about the token in use stay.
        assert text.count("{confirmed}") == 1 and text.endswith("{note}"), (path, step)
    english = _load(PACKAGE / "strings.json")["options"]["step"]
    assert (
        "cannot be revoked from Home Assistant"
        in english["local_tokens"]["description"]
    )
    assert "usually remain" not in english["local_tokens"]["description"]
    assert (
        "cannot be revoked from Home Assistant"
        in english["local_tokens_remove"]["description"]
    )


def test_the_icom_password_hint_names_who_sets_the_private_password():
    options = _load(PACKAGE / "strings.json")["options"]
    for text in (
        options["error"]["icom_invalid_auth"],
        options["step"]["connection_test_auth_icom"]["description"],
        options["step"]["icom_access"]["description"],
        options["step"]["icom_access"]["data_description"]["private_password"],
    ):
        # The personal password is set when the gateway is paired in the app
        # or in the old device app; no text claims the app never sets it.
        assert text.count("when pairing") == 1, text
        assert "old device app" in text
        assert "never sets" not in text and "never saved" not in text


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_both_icom_password_hints_give_the_same_explanation(path):
    options = _load(path)["options"]
    hints = [
        options["error"]["icom_invalid_auth"],
        options["step"]["connection_test_auth_icom"]["description"],
    ]
    # The explanation after the cause label is the same in both texts and
    # names the pairing, setting it again and the display password.
    tails = [hint.rsplit(":", 1)[1].strip() for hint in hints]
    assert tails[0] == tails[1], path
    assert tails[0].count(". ") >= 3, path
