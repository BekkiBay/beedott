from datetime import datetime

from daemon.sinks.note import NoteSink


def test_note_sink_writes_md(tmp_path):
    sink = NoteSink(name="note", base_dir=tmp_path / "md")
    res = sink.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")
    assert res.success and len(list((tmp_path / "md").rglob("*.md"))) == 1


def test_obsidian_variant_adds_frontmatter(tmp_path):
    sink = NoteSink(name="obsidian", base_dir=tmp_path / "v", frontmatter=True)
    sink.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
              session_id="abc")
    md = next((tmp_path / "v").rglob("*.md")).read_text()
    assert md.startswith("---")


def test_note_and_obsidian_are_independent_instances(tmp_path):
    note = NoteSink(name="note", base_dir=tmp_path / "md")
    obsidian = NoteSink(name="obsidian", base_dir=tmp_path / "vault" / "Meetings",
                        frontmatter=True)

    r1 = note.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                   session_id="abc")
    r2 = obsidian.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                       session_id="abc")

    assert r1.name == "note" and r2.name == "obsidian"
    assert len(list((tmp_path / "md").rglob("*.md"))) == 1
    assert len(list((tmp_path / "vault").rglob("*.md"))) == 1
    # plain note-вариант без frontmatter
    assert not (tmp_path / "md" / "2026-07-13" / "1430-встреча.md").read_text().startswith("---")


def test_note_sink_empty_text_fails(tmp_path):
    sink = NoteSink(name="note", base_dir=tmp_path / "md")
    res = sink.send("", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")
    assert not res.success
