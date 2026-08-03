from pullbox_provider_contract.search_terms import (
    collection_title_fragment,
    is_collection_intent,
)


def test_collection_title_fragment_preserves_plain_meaningful_title() -> None:
    assert collection_title_fragment("The End of All Songs") == "The End of All Songs"


def test_collection_title_fragment_ignores_generic_and_number_only_titles() -> None:
    assert collection_title_fragment("HC/TPB") is None
    assert collection_title_fragment("Volume 3") is None


def test_collection_title_fragment_ignores_prefix_without_subtitle() -> None:
    assert collection_title_fragment("Vol. 3:") is None


def test_collection_intent_is_case_insensitive_and_null_safe() -> None:
    assert is_collection_intent("TPB") is True
    assert is_collection_intent(None) is False
