from fastapi.testclient import TestClient

from 主程序.repositories.database import initialize_database, upgrade_database
from 主程序.web.app import create_app


def test_tc60_counts_evidence_is_read_only_and_missing_batch_not_zero(tmp_path):
    db = tmp_path / "isolated.sqlite3"
    initialize_database(db)
    upgrade_database(db)
    before = db.read_bytes()
    with TestClient(create_app(db)) as client:
        page = client.get("/evidence/counts")
        assert page.status_code == 200
        assert "尚未完成" in page.text
        assert client.get("/static/js/counts.js").status_code == 200
        missing = client.get("/evidence/counts/not-a-batch")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "BATCH_NOT_FOUND"
        assert str(db) not in missing.text
        assert client.post("/evidence/counts/not-a-batch").status_code == 405
        assert client.post("/api/v1/imports").status_code == 404
        assert client.post("/api/v1/runs").status_code == 404
        assert client.get("/static/js/unknown.js").status_code == 404
    assert db.read_bytes() == before
