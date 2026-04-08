from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "index.html"


def test_personal_groups_treat_teacher_404_as_empty_state():
    source = FRONTEND.read_text(encoding="utf-8")

    assert "async function fetchPersonalGroupsData(staffId, options = {}) {" in source
    assert "if (response.status === 404) {" in source
    assert "personalGroupsDataCache.set(cacheKey, {" in source
    assert "items: []" in source
