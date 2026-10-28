from fastapi.testclient import TestClient
from server import app
from starters import CATEGORIES, SEARCH_STARTERS


def test_search_starters_are_unique_focused_queries():
    assert len(SEARCH_STARTERS) >= 60
    assert len(SEARCH_STARTERS) == len(set(SEARCH_STARTERS))
    for query in SEARCH_STARTERS:
        assert query == query.strip()
        assert 1 <= len(query) <= 512
        assert not any(character.isspace() for character in query)
        assert "\x00" not in query
    assert {"独山县", "正威集团", "社会化抚养"} <= set(SEARCH_STARTERS)


def test_ai_starters_keep_eight_categories_and_unique_questions():
    assert len(CATEGORIES) == 8
    assert len({category["name"] for category in CATEGORIES}) == 8
    questions = [
        topic["question"] for category in CATEGORIES for topic in category["topics"]
    ]
    assert len(questions) >= 160
    assert len(questions) == len(set(questions))
    assert all(0 < len(question) <= 2000 for question in questions)
    assert all(len(category["topics"]) >= 19 for category in CATEGORIES)


def test_starter_endpoint_serves_the_full_libraries():
    with TestClient(app) as client:
        response = client.get("/api/starters")
    assert response.status_code == 200
    assert response.json() == {"categories": CATEGORIES, "search": SEARCH_STARTERS}
