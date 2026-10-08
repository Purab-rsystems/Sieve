from sieve.committee.qualify import qualify

MEMBERS = [
    {"title": "Chief Financial Officer", "function": "Finance", "role": "Economic Buyer", "judge_p": 0.9,
     "checker_p": 0.8, "chance": 0.05, "judge_reasoning": "Finance holds the budget. It signs off. More.",
     "checker_reasoning": "The CFO decides. Yes."},
    {"title": "Chief Technology Officer", "function": "Technology", "role": "Champion", "judge_p": 0.7,
     "checker_p": 0.6, "chance": 0.05, "judge_reasoning": "x", "checker_reasoning": "y"},
]


def test_qualifies_only_when_every_line_is_met_with_proof():
    q = qualify(MEMBERS, {"Economic Buyer": 1, "Champion": 1})
    assert q["qualified"] and not q["failed"]
    eb = q["lines"][0]
    assert eb["proof"][0]["who"] == "Chief Financial Officer"
    assert eb["proof"][0]["judge_quote"] == "Finance holds the budget. It signs off."     # first two sentences
    q = qualify(MEMBERS, {"Economic Buyer": 1, "Champion": 1, "Technical Evaluator": 1, "Influencer": 3})
    assert not q["qualified"] and q["failed"] == ["Technical Evaluator: 0 of 1", "Influencer: 0 of 3"]


def test_zero_is_met_by_nobody():
    assert qualify([], {"Influencer": 0})["qualified"]
