import storage
from storage import State


def test_state_persists(tmp_path):
    path = tmp_path / "state.json"
    s = State(str(path), banned=[1], ignore_words=["спам"])
    assert s.ban(2)
    assert not s.ban(2)
    assert s.add_word("Реклама")
    s.set_paused(True)
    s.inc("errors")
    s.link(-100, 5, 777, 888, wh=False)
    s.save()

    s2 = State(str(path))
    assert s2.banned == {1, 2}
    assert s2.ignore_words == ["спам", "реклама"]
    assert s2.paused
    assert s2.stats["errors"] == 1
    assert s2.get_dc(-100, 5)["m"] == 888
    assert s2.get_tg(888) == {"c": -100, "m": 5}


def test_unlink_and_trim(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_LINKS", 3)
    s = State(str(tmp_path / "s.json"))
    for i in range(5):
        s.link(1, i, 10, 100 + i)
    assert len(s.tg_to_dc) == 3
    assert s.get_dc(1, 0) is None
    assert s.unlink(1, 4)["m"] == 104
    assert s.get_tg(104) is None


def test_corrupt_file_ignored(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    s = State(str(path), banned=[7])
    assert s.banned == {7}
