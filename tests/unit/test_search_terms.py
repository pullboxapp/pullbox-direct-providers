import inspect

import pullbox_provider_contract.search_terms as search_terms
from pullbox_provider_contract.search_terms import (
    collection_title_fragment,
    collection_title_number,
    is_collection_intent,
)


def test_collection_title_fragment_preserves_plain_meaningful_title() -> None:
    assert collection_title_fragment("The End of All Songs") == "The End of All Songs"


def test_collection_title_fragment_ignores_generic_and_number_only_titles() -> None:
    assert collection_title_fragment("HC/TPB") is None
    assert collection_title_fragment("Volume 3") is None


def test_collection_title_fragment_ignores_prefix_without_subtitle() -> None:
    assert collection_title_fragment("Vol. 3:") is None


def test_collection_prefix_variants_preserve_existing_semantics() -> None:
    assert collection_title_fragment("Vol. #3: The End of All Songs") == (
        "Vol 3 The End of All Songs"
    )
    assert collection_title_fragment("volume 2 - Exile") == "Vol 2 Exile"
    assert collection_title_fragment("Book4\u2014Golden Dawn") == "Book 4 Golden Dawn"
    assert collection_title_fragment("Part 1.5 \u2013 Finale") == "Part 1.5 Finale"
    assert collection_title_number("Book4\u2014Golden Dawn") == "4"
    assert collection_title_number("Part 1.5 \u2013 Finale") == "1.5"


def test_collection_prefix_parser_handles_adversarial_spacing_linearly() -> None:
    value = f"Book0{' ' * 100_000}"

    assert collection_title_fragment(value) is None
    assert collection_title_number(value) == "0"


def test_collection_prefix_parser_does_not_use_backtracking_regex() -> None:
    source = inspect.getsource(search_terms)

    assert "_COLLECTION_PREFIX_RE" not in source


def test_collection_intent_is_case_insensitive_and_null_safe() -> None:
    assert is_collection_intent("TPB") is True
    assert is_collection_intent(None) is False
