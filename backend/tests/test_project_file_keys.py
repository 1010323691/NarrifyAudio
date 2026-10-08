"""Portable file-key indexing for ORM, batched inserts and project relocation."""
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session
from backend.platform.database import Base
from backend.platform.models import ProjectFile, Project, User
from backend.core.object_keys import object_key_lookup_key


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'keys.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="owner", username="owner", email="keys@example.test", password_hash="unused"))
        db.flush()
        db.add(Project(id="project", owner_id="owner", name="Book", directory_key="owner/project"))
        db.commit()
        yield db
    engine.dispose()


def file_values(key):
    return dict(project_id="project", owner_id="owner", original_name=key.split("/")[-1], object_key=key,
                sha256="a" * 64, size_bytes=1)


def test_normalized_index_preserves_collision_rows_and_unicode(db):
    keys = ["owner/project/03_parsed_json/Straße.json", "owner/project/03_parsed_json/STRASSE.json",
            "owner/project/03_parsed_json/é.json", "owner/project/03_parsed_json/e\u0301.json"]
    db.execute(insert(ProjectFile), [file_values(key) for key in keys])
    db.commit()
    for key in (keys[0], keys[2]):
        matches = db.scalars(select(ProjectFile).where(ProjectFile.project_id == "project",
            ProjectFile.object_key_normalized == object_key_lookup_key(key))).all()
        assert len(matches) == 2
        assert all(row.object_key_normalized == object_key_lookup_key(row.object_key) for row in matches)
    assert len(db.scalars(select(ProjectFile)).all()) == 4


def test_orm_relocation_and_metadata_updates_keep_index(db):
    item = ProjectFile(**file_values("owner/project/03_parsed_json/OLD.JSON"))
    db.add(item)
    db.commit()
    item.object_key = "owner/renamed/03_parsed_json/New.json"
    item.size_bytes = 3
    db.commit()
    db.expire_all()
    assert db.get(ProjectFile, item.id).object_key_normalized == object_key_lookup_key("owner/renamed/03_parsed_json/new.json")
    item.sha256 = "b" * 64
    db.commit()
    assert item.object_key_normalized == object_key_lookup_key(item.object_key)


def test_expanding_unicode_legacy_key_has_bounded_index(db):
    key = "owner/project/03_parsed_json/" + "ΐ" * 600 + ".json"
    assert len(key) < 700
    from backend.core.object_keys import normalized_object_key
    assert len(normalized_object_key(key)) > 700
    db.execute(insert(ProjectFile), [file_values(key)])
    db.commit()
    row = db.scalar(select(ProjectFile).where(ProjectFile.object_key_normalized == object_key_lookup_key(key)))
    assert row.object_key == key and len(row.object_key_normalized) == 64
