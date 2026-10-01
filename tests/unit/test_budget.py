from collections.abc import Callable

from pydantic import BaseModel

from mcp_hub.budget import HARD_MAX_CHARS, fit_items, serialized_length, shrink_or_drop, shrink_to_fit
from mcp_hub.sanitize import TRUNCATION_MARKER


class U(BaseModel):
    subject: str
    body: str


class Item(BaseModel):
    id: str
    untrusted: U


class Envelope(BaseModel):
    items: list[Item]
    truncated: bool


def item(n: int, size: int = 100) -> Item:
    return Item(id=f"i{n}", untrusted=U(subject="s", body="b" * size))


def builder(provider_truncated: bool = False) -> Callable[[list[Item], bool], Envelope]:
    def build(items: list[Item], cut: bool) -> Envelope:
        return Envelope(items=items, truncated=provider_truncated or cut)

    return build


def test_everything_fits_under_the_budget() -> None:
    result = fit_items([item(i) for i in range(3)], builder(), 10_000)
    assert [i.id for i in result.items] == ["i0", "i1", "i2"]
    assert result.truncated is False


def test_drops_items_from_the_end_and_sets_truncated() -> None:
    items = [item(i, 1000) for i in range(10)]
    result = fit_items(items, builder(), 3500)
    assert 0 < len(result.items) < 10
    assert [i.id for i in result.items] == [f"i{n}" for n in range(len(result.items))]
    assert result.truncated is True
    assert serialized_length(result) <= 3500


def test_provider_truncated_flag_is_kept() -> None:
    assert fit_items([item(0)], builder(provider_truncated=True), 10_000).truncated is True


def test_single_oversized_item_shortens_its_longest_untrusted_field() -> None:
    result = fit_items([item(0, 50_000)], builder(), 5_000)
    assert len(result.items) == 1
    assert result.items[0].untrusted.body.endswith(TRUNCATION_MARKER)
    assert result.items[0].untrusted.subject == "s"
    assert result.truncated is True
    assert serialized_length(result) <= 5_000


def test_hard_maximum_caps_a_larger_budget() -> None:
    result = fit_items([item(i, 20_000) for i in range(10)], builder(), 10**9)
    assert serialized_length(result) <= HARD_MAX_CHARS


def test_shrink_to_fit_gives_up_below_the_minimum() -> None:
    assert shrink_to_fit(item(0, 10), lambda m: False) is None


class Listed(BaseModel):
    names: list[str]
    untrusted: U


def drop_name(model: Listed) -> Listed | None:
    return model.model_copy(update={"names": model.names[:-1]}) if model.names else None


def test_shrink_or_drop_drops_list_entries_until_the_shrunk_model_fits() -> None:
    model = Listed(names=["n" * 200] * 40, untrusted=U(subject="s", body="b" * 20_000))
    fitted = shrink_or_drop(model, lambda m: serialized_length(m) <= 3_000, drop_name)
    assert fitted is not None
    assert serialized_length(fitted) <= 3_000
    assert 0 < len(fitted.names) < 40  # entries were dropped only as far as needed


def test_shrink_or_drop_keeps_every_entry_when_shrinking_is_enough() -> None:
    model = Listed(names=["n"] * 5, untrusted=U(subject="s", body="b" * 20_000))
    fitted = shrink_or_drop(model, lambda m: serialized_length(m) <= 2_000, drop_name)
    assert fitted is not None
    assert len(fitted.names) == 5


def test_shrink_or_drop_gives_up_when_nothing_is_left() -> None:
    assert shrink_or_drop(Listed(names=["a"], untrusted=U(subject="s", body="b")), lambda m: False, drop_name) is None
