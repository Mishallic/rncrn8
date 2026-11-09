import json

import pytest

from rncrn8.packet import ReviewPacket, Section, load_packet, packet_from_text


def test_markdown_headings_become_sections_with_refs():
    text = "# Title\n\nintro para\n\n## Second\n\nbody two\n\n## Third\n\nbody three\n"
    p = packet_from_text(text, "doc")
    assert [s.ref for s in p.sections] == ["§1", "§2", "§3"]
    assert p.sections[0].title == "Title" and "intro para" in p.sections[0].text
    assert p.sections[2].text == "body three"
    rendered = p.render()
    assert "[§2] Second" in rendered and "KIND: document" in rendered and "TITLE: doc" in rendered


def test_plain_text_is_chunked_by_paragraphs():
    text = "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(10))
    p = packet_from_text(text, "plain", max_section_chars=1000)
    assert len(p.sections) > 1
    assert all(len(s.text) <= 1000 + 10 for s in p.sections)
    assert p.sections[0].ref == "§1"


def test_long_heading_section_is_split_into_parts():
    text = "# Big\n\n" + "\n\n".join("y" * 500 for _ in range(8))
    p = packet_from_text(text, "doc", max_section_chars=1200)
    refs = [s.ref for s in p.sections]
    assert refs[0] == "§1.1" and refs[1] == "§1.2"
    assert p.sections[0].title == "Big" and p.sections[1].title is None


def test_empty_text_gives_one_empty_section():
    p = packet_from_text("", "empty")
    assert len(p.sections) == 1 and p.sections[0].text == ""


def test_load_packet_text_and_json(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# A\n\nhello\n")
    p = load_packet(md)
    assert p.title == "a" and p.kind == "document" and p.sections[0].text == "hello"
    p2 = load_packet(md, kind="idea", title="Pitch")
    assert p2.kind == "idea" and p2.title == "Pitch"

    j = tmp_path / "cut.packet.json"
    packet = ReviewPacket(kind="video", title="cut 7", sections=[Section(ref="t=00:00", text="wide shot")], metadata={"duration": "90s"})
    j.write_text(packet.model_dump_json())
    loaded = load_packet(j)
    assert loaded == packet
    assert "DURATION: 90s" in loaded.render() and "[t=00:00]" in loaded.render()
    assert load_packet(j, kind="other").kind == "other"


def test_load_packet_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_packet(tmp_path / "missing.md")
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(ValueError):
        load_packet(d)
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError):
        load_packet(binary)


def test_headings_inside_fenced_code_do_not_split_sections():
    text = "# Real\n\n```bash\n# a comment, not a heading\nrncrn8 init\n```\n\n~~~\n## also not a heading\n~~~\n\n## Second\n\nbody\n"
    p = packet_from_text(text, "doc")
    assert [s.title for s in p.sections] == ["Real", "Second"]
    assert "# a comment, not a heading" in p.sections[0].text and "## also not a heading" in p.sections[0].text
