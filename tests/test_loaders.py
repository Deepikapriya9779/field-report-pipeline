from conftest import RAW

from fieldreports.loaders import discover, file_sha256, load, load_text


def test_discover_finds_every_supported_file_in_name_order():
    files = discover(RAW)
    assert len(files) == 32
    assert [f.name for f in files] == sorted(f.name for f in files)
    assert files[0].name.startswith("DR_")  # uppercase sorts first, on Windows too
    assert {f.suffix for f in files} == {".txt", ".eml", ".pdf"}


def test_email_keeps_sender_and_subject_but_drops_the_date_header():
    text = load_text(RAW / "Daily - Harbor Pt MOB - 9-30.eml")
    assert text.startswith("From: Marcus Reyes")
    assert "Subject: Daily - Harbor Pt MOB - 9/30/26" in text
    assert "Date:" not in text  # sent 00:42 on 10/1; must not leak into report_date


def test_email_body_is_decoded_from_quoted_printable():
    text = load_text(RAW / "Daily - Harbor Pt MOB - 9-30.eml")
    assert "(2.5 hrs lost)" in text
    assert "=\n" not in text


def test_pdf_text_layer_is_extracted():
    text = load_text(RAW / "DR_2026-09-23_HarborPoint.pdf")
    assert "Report Date: 2026-09-23" in text


def test_crlf_and_lf_files_give_the_same_text(tmp_path):
    lf, crlf = tmp_path / "a.txt", tmp_path / "b.txt"
    lf.write_bytes(b"line one\nline two\n")
    crlf.write_bytes(b"line one\r\nline two\r\n")
    assert load_text(lf) == load_text(crlf)
    assert file_sha256(lf) != file_sha256(crlf)  # the fingerprint is over raw bytes


def test_hash_is_stable_across_renames(tmp_path):
    src = RAW / "daily 9-25.txt"
    copy = tmp_path / "renamed.txt"
    copy.write_bytes(src.read_bytes())
    assert load(copy).sha256 == load(src).sha256
