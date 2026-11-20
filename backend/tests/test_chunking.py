from app.domain.models import ParsedPage
from app.rag.chunker import chunk_pages, chunk_text


def test_page_aware_chunks_keep_global_offsets_and_clause_titles():
    pages = [
        ParsedPage(page=1, text="1. Payment Terms\nPayment is due within 30 days. " * 30),
        ParsedPage(page=2, text="2. Liability\nThe liability cap is stated here. " * 30),
    ]

    chunks = chunk_pages(pages, "doc_1", "ver_1", max_chars=500, overlap=50, max_tokens=100)

    assert chunks
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert {chunk.page for chunk in chunks} == {1, 2}
    assert any(chunk.section_path == "1. Payment Terms" for chunk in chunks)
    assert any(chunk.section_path == "2. Liability" for chunk in chunks)
    assert all(chunk.char_start < chunk.char_end for chunk in chunks)
    assert all(chunk.token_estimate <= 100 for chunk in chunks)


def test_trimmed_chunk_offsets_point_to_the_original_text():
    text = "   1. Payment Terms\n  Pay within 30 days.   "
    chunk = chunk_text(text, "doc_1", "ver_1", max_chars=200, overlap=20)[0]

    assert text[chunk.char_start:chunk.char_end] == chunk.text


def test_empty_pages_do_not_break_global_offsets():
    pages = [ParsedPage(page=1, text=""), ParsedPage(page=2, text="2. Liability\nCap applies.")]
    chunks = chunk_pages(pages, "doc_1", "ver_1", max_chars=200, overlap=20, max_tokens=100)

    full_text = "\n\n".join(page.text for page in pages)
    assert chunks
    assert full_text[chunks[0].char_start:chunks[0].char_end] == chunks[0].text
    assert chunks[0].page == 2
