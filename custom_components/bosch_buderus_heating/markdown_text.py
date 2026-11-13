"""Show outside text literally inside a Markdown dialog description (D43).

Home Assistant renders the description of a dialog step as Markdown. A
holiday name can come from the cloud or from any user of the calendar
service; without escaping, a name like ``![](https://...)`` loads a remote
image when an administrator opens the dialog, and ``[text](url)`` shows a
foreign link. Each Markdown sign and the start of a bare link are escaped
with a backslash, which the renderer removes again, so the text reads
exactly as given. Field values and selection labels are plain text and must
not use this helper.
"""

from __future__ import annotations

import re

# Markdown and HTML signs; "@" for e-mail autolinks, "&" for entities.
_SIGNS = re.compile(r"([\\`*_{}\[\]()!#|<>~&@])")
# Bare links: "https://..." and "www.". Dates and times keep their ":".
_SCHEME = re.compile(r"(?<=[A-Za-z0-9]):(?=//)")
_WWW = re.compile(r"\b(www)\.", re.IGNORECASE)
MAX_LENGTH = 240


def markdown_literal(value: str) -> str:
    """One line of outside text, bounded, that renders as itself."""
    text = " ".join(str(value).split())[:MAX_LENGTH]
    text = _SIGNS.sub(r"\\\1", text)
    text = _SCHEME.sub(r"\\:", text)
    return _WWW.sub(r"\1\\.", text)
