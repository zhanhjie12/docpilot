"""Indexing helpers for persisted lexical chunks."""

from __future__ import annotations

from app.infrastructure.database import SQLiteDatabase
from app.rag.chunker import Chunk


def replace_version_chunks(db: SQLiteDatabase, chunks: list[Chunk]) -> int:
    """原子语义地替换一个版本的块记录，返回写入数量。"""

    if not chunks:
        return 0
    # 重建索引前先删除旧块，避免同一版本出现重复检索结果。
    db.run("DELETE FROM chunks WHERE version_id=?", (chunks[0].version_id,))
    for chunk in chunks:
        db.run("INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,content_sha256,section_path,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))", (chunk.chunk_id, chunk.document_id, chunk.version_id, chunk.page, chunk.text, chunk.char_start, chunk.char_end, chunk.content_sha256, chunk.section_path, chunk.token_estimate))
    return len(chunks)
