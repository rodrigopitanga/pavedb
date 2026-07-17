# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from pave.filters import matches_filters, sanit_field


@pytest.mark.parametrize("raw, expected", [
    ("first name", "first_name"),     # space -> _
    ("first  name", "first_name"),    # run of spaces collapses to one _
    ("a__b", "a_b"),                  # literal underscores collapse
    ("a.b/c", "a_b_c"),               # punctuation -> _
    (" x ", "x"),                     # leading/trailing trimmed
    ("__lead_trail__", "lead_trail"), # underscores trimmed
    ("café", "café"),                 # Unicode letters preserved
    ("first-name", "first-name"),     # hyphen preserved (intended asymmetry)
    ("!!!", ""),                      # all-punctuation -> empty
    ("docid", "docid"),
    (".docid.", "docid"),             # still sanitizes INTO a reserved slot
    (" text ", "text"),
    (5, "5"),                         # non-str coerced
])
def test_sanit_field_collapses_disallowed_to_underscore(raw, expected):
    assert sanit_field(raw) == expected


def test_string_not_equal_filter_matches_non_equal_value():
    assert matches_filters({"status": "published"}, {"status": ["!=draft"]})
    assert not matches_filters({"status": "draft"}, {"status": ["!=draft"]})
