"""Layer 5: restricted-column dropping, PII masking, preview truncation, JSON safety."""

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from text2sql.output_check import EMAIL_MASK, PHONE_MASK, RESTRICTED_MASK, check, to_jsonable

RESTRICTED = {"customer_zip_code_prefix", "review_comment_message"}


def test_restricted_columns_are_dropped():
    out = check(
        ["customer_city", "CUSTOMER_ZIP_CODE_PREFIX"],
        [("sao paulo", "01001")],
        restricted_names=RESTRICTED,
    )
    assert out.ok
    assert out.columns == ["customer_city"] and out.rows == [["sao paulo"]]
    assert out.dropped_columns == ["CUSTOMER_ZIP_CODE_PREFIX"]


def test_all_columns_restricted_is_refused():
    out = check(["customer_zip_code_prefix"], [("01001",)], restricted_names=RESTRICTED)
    assert not out.ok and "restricted" in out.reason
    assert out.rows == [] and out.columns == []


def test_default_restricted_names_come_from_policy():
    out = check(["customer_zip_code_prefix", "n"], [("01001", 1)])
    assert out.columns == ["n"]


@pytest.mark.parametrize(
    "cell, expected",
    [
        ("contact: maria.silva@example.com.br", f"contact: {EMAIL_MASK}"),
        ("JOAO+test@mail.co", EMAIL_MASK),
        ("ligue (11) 98765-4321 hoje", f"ligue {PHONE_MASK} hoje"),
        ("+55 11 98765 4321", PHONE_MASK),
        ("tel 21 3456-7890", f"tel {PHONE_MASK}"),
        ("+1 415.555.0123", PHONE_MASK),
        ("a@b.io or (21) 99999-0000", f"{EMAIL_MASK} or {PHONE_MASK}"),
    ],
)
def test_pii_is_masked(cell, expected):
    out = check(["text"], [(cell,)], restricted_names=RESTRICTED)
    assert out.rows == [[expected]] and out.masked_cells == 1


@pytest.mark.parametrize(
    "cell",
    [
        "8d50f5eadf50201ccdcedfb9e2ac8455",  # Olist hex id
        "e481f51cbdc54678b7cc49136f2d6af7",
        "2017-10-02 10:56:33",  # timestamp as text
        "2017-10",
        "01001",  # zip prefix
        "sao paulo",
        "health_beauty",
        "R$ 1234.56",
    ],
)
def test_no_false_positives_on_dataset_values(cell):
    out = check(["v"], [(cell,)], restricted_names=RESTRICTED)
    assert out.rows == [[cell]] and out.masked_cells == 0


def test_numbers_are_never_masked():
    out = check(["n"], [(11987654321,)], restricted_names=RESTRICTED)
    assert out.rows == [[11987654321]]


def test_preview_is_truncated_to_20_rows():
    rows = [(i,) for i in range(57)]
    out = check(["i"], rows, restricted_names=RESTRICTED)
    assert len(out.rows) == 20 and out.rows[-1] == [19]
    assert out.row_count == 57 and out.truncated
    small = check(["i"], rows[:20], restricted_names=RESTRICTED)
    assert small.row_count == 20 and not small.truncated


def test_nested_values_are_masked_and_restricted_keys_hidden():
    struct = {"customer_city": "x", "customer_zip_code_prefix": "01001", "mail": "a@b.com"}
    out = check(["s", "l"], [(struct, ["c@d.org", 1])], restricted_names=RESTRICTED)
    assert out.rows == [
        [{"customer_city": "x", "customer_zip_code_prefix": RESTRICTED_MASK, "mail": EMAIL_MASK},
         [EMAIL_MASK, 1]]
    ]  # fmt: skip
    assert out.masked_cells == 3


def test_cells_are_json_safe():
    row = (
        Decimal("12.50"),
        dt.datetime(2017, 1, 2, 3, 4, 5),
        dt.date(2017, 1, 2),
        dt.timedelta(days=1),
        float("nan"),
        b"\x00\x01",
        uuid.UUID(int=1),
        None,
        True,
        object(),
    )
    out = check([f"c{i}" for i in range(len(row))], [row], restricted_names=RESTRICTED)
    json.dumps(out.result())  # must not raise
    assert out.rows[0][:9] == [
        12.5, "2017-01-02T03:04:05", "2017-01-02", 86400.0, None, "<2 bytes>",
        "00000000-0000-0000-0000-000000000001", None, True,
    ]  # fmt: skip
    assert to_jsonable(float("inf")) is None


def test_result_and_details_shape():
    out = check(["a"], [(1,)], restricted_names=RESTRICTED)
    assert out.result() == {"columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": False}
    assert set(out.details()) == {
        "dropped_columns", "masked_cells", "preview_rows", "row_count", "truncated"
    }  # fmt: skip


def test_empty_result():
    out = check([], [], restricted_names=RESTRICTED)
    assert out.ok and out.row_count == 0 and out.rows == []
