import pytest
from docx import Document

from eval.harness.corpus import (
    build_corpus_manifest, extract_text, list_pool, select_sample, sha256_file, stage_corpus,
)


def make_files(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(name)


def make_pool(tmp_path):
    names = [f"paper_{i:02d}.pdf" for i in range(30)] + [f"distributed_{i}.pdf" for i in range(5)]
    make_files(tmp_path / "pool", names)
    return list_pool([tmp_path / "pool"])


def test_list_pool_filters_suffix_sorts_and_rejects_duplicates(tmp_path):
    make_files(tmp_path / "a", ["b.pdf", "a.pdf", "notes.txt"])
    assert [p.name for p in list_pool([tmp_path / "a", tmp_path / "missing"])] == ["a.pdf", "b.pdf"]
    make_files(tmp_path / "b", ["a.pdf"])
    with pytest.raises(ValueError, match="duplicate"):
        list_pool([tmp_path / "a", tmp_path / "b"])


def test_select_sample_is_deterministic_disjoint_and_keyword_driven(tmp_path):
    pool = make_pool(tmp_path)
    first = select_sample(pool, seed=1, n_random=10, n_hard=3)
    assert first == select_sample(pool, seed=1, n_random=10, n_hard=3)
    assert len(first["hard_negatives"]) == 3 and all("distributed" in n for n in first["hard_negatives"])
    assert len(first["random"]) == 10
    assert not set(first["hard_negatives"]) & set(first["random"])
    assert first["random"] != select_sample(pool, seed=2, n_random=10, n_hard=3)["random"]


def test_select_sample_caps_hard_negatives_and_honours_include(tmp_path):
    pool = make_pool(tmp_path)
    capped = select_sample(pool, seed=1, n_random=5, n_hard=50)
    assert len(capped["hard_negatives"]) == 5
    forced = select_sample(pool, seed=1, n_random=29, n_hard=0, include=["paper_00.pdf"])
    assert forced["forced"] == ["paper_00.pdf"] and "paper_00.pdf" not in forced["random"]
    with pytest.raises(ValueError, match="not in the pool"):
        select_sample(pool, seed=1, n_random=1, n_hard=0, include=["nope.pdf"])


def test_manifest_and_staging_verify_hashes(tmp_path):
    pool = make_pool(tmp_path)
    make_files(tmp_path / "true", ["k8s.txt"])
    selection = select_sample(pool, seed=1, n_random=2, n_hard=1)
    manifest = build_corpus_manifest(tmp_path / "true", pool, selection, seed=1)
    assert manifest["seed"] == 1 and manifest["true_data"] == [{"filename": "k8s.txt", "sha256": sha256_file(tmp_path / "true" / "k8s.txt")}]
    assert stage_corpus(manifest, tmp_path / "true", pool, tmp_path / "stage") == 4
    assert (tmp_path / "stage" / "k8s.txt").exists()
    (tmp_path / "pool" / selection["random"][0]).write_text("changed")
    with pytest.raises(ValueError, match="changed since"):
        stage_corpus(manifest, tmp_path / "true", list_pool([tmp_path / "pool"]), tmp_path / "stage2")


def test_extract_text_for_text_html_and_docx(tmp_path):
    (tmp_path / "a.txt").write_text("plain words")
    (tmp_path / "b.html").write_text("<html><body><h1>Title</h1><p>Body text</p></body></html>")
    doc = Document()
    doc.add_paragraph("Horizontal pod autoscaler")
    doc.save(str(tmp_path / "c.docx"))
    assert extract_text(tmp_path / "a.txt") == "plain words"
    assert "Body text" in extract_text(tmp_path / "b.html")
    assert "Horizontal pod autoscaler" in extract_text(tmp_path / "c.docx")
