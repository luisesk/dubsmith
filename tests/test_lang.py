"""ISO 639-1 / 639-2 / BCP 47 normalization."""
from src.lang import lang_matches, normalize


def test_normalize_2_to_3():
    assert normalize("pt") == "por"
    assert normalize("en") == "eng"
    assert normalize("ja") == "jpn"


def test_normalize_bcp47_strips_region():
    assert normalize("pt-BR") == "por"
    assert normalize("en-US") == "eng"


def test_normalize_b_to_t():
    # 639-2/B (bibliographic) -> /T (terminological)
    assert normalize("ger") == "deu"
    assert normalize("fre") == "fra"
    assert normalize("dut") == "nld"


def test_normalize_passthrough_unknown():
    assert normalize("xxx") == "xxx"


def test_normalize_empty():
    assert normalize("") == ""
    assert normalize(None) == ""


def test_lang_matches_cross_form():
    assert lang_matches("pt", "por")
    assert lang_matches("pt-BR", "por")
    assert lang_matches("ger", "deu")
    assert lang_matches("ja", "jpn")


def test_lang_matches_rejects_unrelated():
    assert not lang_matches("eng", "por")
    assert not lang_matches("", "")  # empty never matches itself
    assert not lang_matches("", "por")
