"""Results shared by the query app and the ship picker (framework-free)."""
from dataclasses import dataclass, field
from typing import Union

# bytes = rendered card; (bytes, caption) = card plus a one-glance text line for AI callers
Reply = Union[str, bytes, tuple[bytes, str]]


@dataclass
class Card:
    html: str
    text: str
    caption: str = ""  # the card's numbers as text, handed to the LLM
    status: str = "ok"
    options: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    """What a query produced, with a status the LLM tool can act on.

    status: ok | help | not_bound | mention_not_bound | candidates | not_found | error | bound | unbound
    | hidden | newbie | quota | tracking | idle | nomatch | noship
    picker: ok | invalid | disabled | cooldown | not_bound | no_owned | no_history | hidden
    | stats_unavailable | empty | catalog_busy | error
    """
    status: str
    text: str = ""  # text reply, or the card's text version
    image: bytes | None = None
    caption: str = ""
    options: list[str] = field(default_factory=list)  # nickname candidates / ship names to retry with

    def reply(self, ai: bool = False) -> "Reply":
        if self.image is None:
            return self.text
        return (self.image, self.caption) if ai and self.caption else self.image
