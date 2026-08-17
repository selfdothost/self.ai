"""model_line / model_version tables — self.ai#131.

cavekit-model-versioning.md R1 (line record) and R2 (version record). The
guards are the substance here: ordering that does not depend on the clock, a
kind constraint that holds at write time, and a parent chain that cannot be
made unwalkable.
"""

import time

import pytest

from selfai_ui.models.model_versions import (
    ModelLineForm,
    ModelLines,
    ModelVersion,
    ModelVersionError,
    ModelVersionForm,
    ModelVersions,
)


def _line(user_id="u1", name="gemma-line", **kwargs):
    return ModelLines.insert_new_line(user_id, ModelLineForm(name=name, **kwargs))


def _base(line_id, commit="c1", parent=None):
    return ModelVersions.insert_new_version(
        line_id,
        ModelVersionForm(kind="base", corpus_commit_id=commit, parent_version_id=parent),
    )


def _adapter(line_id, parent, base=None, commit="c2"):
    return ModelVersions.insert_new_version(
        line_id,
        ModelVersionForm(
            kind="adapter",
            corpus_commit_id=commit,
            parent_version_id=parent,
            base_version_id=base,
        ),
    )


####################
# R1 — the line record
####################


def test_insert_line_has_no_current_version_yet(db_session):
    line = _line()
    assert line is not None
    assert line.current_version_id is None
    assert line.user_id == "u1"


def test_line_records_its_corpus_repo(db_session):
    line = _line(corpus_repo="selfai-line-abc")
    assert ModelLines.get_line_by_corpus_repo("selfai-line-abc").id == line.id


@pytest.mark.parametrize("access_control", [None, {}, {"read": {"group_ids": ["g1"], "user_ids": []}}])
def test_access_control_round_trips(db_session, access_control):
    line = _line(access_control=access_control)
    assert ModelLines.get_line_by_id(line.id).access_control == access_control


def test_get_lines_by_user_id_filters(db_session):
    _line(user_id="u1")
    _line(user_id="u2")
    assert [line.user_id for line in ModelLines.get_lines_by_user_id("u1")] == ["u1"]


####################
# R1 — ordering is explicit, not chronological
####################


def test_history_is_ordered_by_sequence_not_insertion(db_session):
    line = _line()
    first = _base(line.id)
    second = _adapter(line.id, parent=first.id)
    third = _adapter(line.id, parent=second.id, commit="c3")

    assert [v.sequence for v in (first, second, third)] == [1, 2, 3]
    history = ModelVersions.get_versions_by_line(line.id)
    assert [v.id for v in history] == [first.id, second.id, third.id]


def test_history_order_survives_identical_created_at(db_session):
    """Two versions written in the same second still have a defined order."""
    line = _line()
    first = _base(line.id)
    second = _adapter(line.id, parent=first.id)

    stamp = int(time.time()) + 500
    db_session.query(ModelVersion).filter_by(line_id=line.id).update({"created_at": stamp})
    db_session.commit()

    history = ModelVersions.get_versions_by_line(line.id)
    assert [v.id for v in history] == [first.id, second.id]
    assert {v.created_at for v in history} == {stamp}


####################
# R2 — the version record
####################


def test_version_carries_its_commit_and_producer(db_session):
    line = _line()
    version = ModelVersions.insert_new_version(
        line.id,
        ModelVersionForm(
            kind="base",
            corpus_commit_id="c0ffee",
            produced_by={"job_kind": "publish", "job_id": "job-1"},
            published_by="u1",
            artifact_ref="gemma-q4.gguf",
        ),
    )
    stored = ModelVersions.get_version_by_id(version.id)
    assert stored.corpus_commit_id == "c0ffee"
    assert stored.produced_by == {"job_kind": "publish", "job_id": "job-1"}
    assert stored.published_by == "u1"
    assert stored.artifact_ref == "gemma-q4.gguf"


def test_version_without_a_commit_is_refused(db_session):
    """A version row must never exist without the commit backing it."""
    line = _line()
    with pytest.raises(ModelVersionError, match="commit"):
        ModelVersions.insert_new_version(line.id, ModelVersionForm(kind="base", corpus_commit_id=""))


def test_invalid_kind_is_refused_at_write_time(db_session):
    """Not only in Pydantic — the table accessor is the enforcement point."""
    line = _line()
    form = ModelVersionForm(kind="base", corpus_commit_id="c1")
    object.__setattr__(form, "kind", "merged")
    with pytest.raises(ModelVersionError, match="invalid version kind"):
        ModelVersions.insert_new_version(line.id, form)
    assert ModelVersions.get_versions_by_line(line.id) == []


def test_version_on_a_missing_line_is_refused(db_session):
    with pytest.raises(ModelVersionError, match="not found"):
        _base("no-such-line")


####################
# R2 — the parent chain cannot be made unwalkable
####################


def test_first_version_has_no_parent(db_session):
    line = _line()
    assert _base(line.id).parent_version_id is None


def test_later_version_must_name_a_parent(db_session):
    line = _line()
    _base(line.id)
    with pytest.raises(ModelVersionError, match="must name its parent"):
        _base(line.id, commit="c2")


def test_parent_from_another_line_is_refused(db_session):
    line_a, line_b = _line(name="a"), _line(name="b")
    foreign = _base(line_a.id)
    with pytest.raises(ModelVersionError, match="different line"):
        _base(line_b.id, parent=foreign.id)


def test_missing_parent_is_refused(db_session):
    line = _line()
    _base(line.id)
    with pytest.raises(ModelVersionError, match="parent version .* not found"):
        _adapter(line.id, parent="ghost")


def test_adapter_base_must_be_a_base_on_the_same_line(db_session):
    line = _line()
    base = _base(line.id)
    adapter = _adapter(line.id, parent=base.id, base=base.id)

    # An adapter's parent may itself be an adapter; its base may not.
    second = _adapter(line.id, parent=adapter.id, base=base.id, commit="c3")
    assert second.parent_version_id == adapter.id
    assert second.base_version_id == base.id

    with pytest.raises(ModelVersionError, match="not of kind 'base'"):
        _adapter(line.id, parent=second.id, base=adapter.id, commit="c4")


####################
# R8 (table half) — the current pointer moves without rebuilding
####################


def test_set_current_version_records_the_move(db_session):
    line = _line()
    first = _base(line.id)
    second = _adapter(line.id, parent=first.id)
    ModelLines.set_current_version(line.id, second.id, moved_by="u1")

    reverted = ModelLines.set_current_version(line.id, first.id, moved_by="u2")
    assert reverted.current_version_id == first.id

    moves = reverted.meta["version_moves"]
    assert [m["to"] for m in moves] == [second.id, first.id]
    assert moves[-1]["from"] == second.id
    assert moves[-1]["by"] == "u2"


def test_revert_creates_no_new_version(db_session):
    line = _line()
    first = _base(line.id)
    second = _adapter(line.id, parent=first.id)
    before = ModelVersions.get_versions_by_line(line.id)

    ModelLines.set_current_version(line.id, first.id)

    after = ModelVersions.get_versions_by_line(line.id)
    assert [v.id for v in after] == [v.id for v in before] == [first.id, second.id]


def test_current_version_must_be_on_this_line(db_session):
    line_a, line_b = _line(name="a"), _line(name="b")
    foreign = _base(line_a.id)
    with pytest.raises(ModelVersionError, match="does not belong"):
        ModelLines.set_current_version(line_b.id, foreign.id)


####################
# R1 — deletion is refused, not cascaded
####################


def test_line_with_versions_cannot_be_deleted(db_session):
    line = _line()
    _base(line.id)
    with pytest.raises(ModelVersionError, match="delete them explicitly"):
        ModelLines.delete_line_by_id(line.id)
    assert ModelLines.get_line_by_id(line.id) is not None


def test_line_deletes_once_its_versions_are_gone(db_session):
    line = _line()
    _base(line.id)
    ModelVersions.delete_versions_by_line(line.id)
    assert ModelLines.delete_line_by_id(line.id) is True
    assert ModelLines.get_line_by_id(line.id) is None
