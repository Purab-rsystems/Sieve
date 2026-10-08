import io

import pandas as pd
import pytest

from sieve.committee.decide import ctx_of, shared_text
from sieve.committee.reference import load_rules
from sieve.committee.rolelist import ListError, fill_descriptions, parse_list, rules_for, template_csv


def test_parse_csv_and_excel_with_flexible_headers():
    rows = parse_list(b"role,MINIMUM,definition\nChampion,2,Pushes for it.\nInfluencer,3,\n", "x.csv")
    assert rows == [{"role": "Champion", "count": 2, "description": "Pushes for it.", "source": "uploaded"},
                    {"role": "Influencer", "count": 3, "description": "", "source": ""}]
    buf = io.BytesIO()
    pd.DataFrame({"Role": ["Champion"], "Count": [1]}).to_excel(buf, index=False)
    assert parse_list(buf.getvalue(), "x.xlsx")[0]["count"] == 1


@pytest.mark.parametrize("data,msg", [
    (b"Role,Count\nChampion,1\nchampion,2\n", "twice"),
    (b"Role,Count\nChampion,11\n", "between 0 and 10"),
    (b"Role,Count\n", "no roles"),
    (b"Who,Count\nChampion,1\n", "Role column"),
])
def test_bad_lists_say_why(data, msg):
    with pytest.raises(ListError, match=msg):
        parse_list(data, "x.csv")


def test_missing_descriptions_are_written_and_marked():
    rows = parse_list(b"Role,Count,Description\nA,1,Given.\nB,1,\n", "x.csv")
    out = fill_descriptions(rows, lambda prompt: "  Written   text. ")
    assert out[0]["description"] == "Given." and out[0]["source"] == "uploaded"
    assert out[1]["description"] == "Written text." and out[1]["source"].startswith("written by Qwen")


def test_rules_for_uploaded_list_and_separate_answers():
    rows = [{"role": "Security Reviewer", "count": 1, "description": "Checks security.", "source": "uploaded"}]
    r = rules_for(rows)
    assert r["minimums"] == {"Security Reviewer": 1} and r["seat_order"] == ["Security Reviewer"]
    assert r["also_asked"] == {}                       # no Influencer role: User/Ratifier are not asked
    assert "- Security Reviewer: Checks security." in shared_text(r)
    assert ctx_of(None) == "" and ctx_of(load_rules()) == "" and ctx_of(r).startswith("ctx:")
    assert rules_for(None) == load_rules()
    assert template_csv().splitlines()[0] == "Role,Count,Description"
