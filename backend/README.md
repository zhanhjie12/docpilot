# DocPilot backend

The backend is a FastAPI service with a local SQLite database, local object
storage and an in-process task executor. It implements these local flows:

- PDF/DOCX upload, SHA-256 deduplication and version metadata
- background parsing, page-aware clause chunking, text extraction and deterministic fact extraction
- task polling and SSE events
- multi-document numeric/risk screening
- evidence-only keyword Q&A with citations
- bounded Agent reviews with verified citations, optimistic human decisions,
  two-sided version comparison and JSON/Markdown/CSV export

## Start

From this directory:

```powershell
python -m pip install -e ".[parsing,dev]"
python -m uvicorn app.main:app --reload
```

The service listens on `http://localhost:8000`; OpenAPI is at `/docs`. Runtime
files are stored under `backend/data` by default. Configure `DATABASE_URL`,
`UPLOAD_DIR`, `CORS_ORIGINS` and `MAX_UPLOAD_BYTES` through the project `.env`.

## Authentication

The backend provides `POST /api/v1/auth/register`,
`POST /api/v1/auth/login` and `GET /api/v1/auth/me`. Passwords are stored as
salted PBKDF2 hashes and successful authentication returns a signed Bearer
token. The frontend stores this token locally and adds it to API requests.

For local development, business routes remain usable without a token. Set
`AUTH_REQUIRED=true` and replace `JWT_SECRET` with a long random value before
deploying a service that requires login. User identity is implemented first;
authenticated documents, tasks, reviews and chat sessions are now scoped to
the creating user. Full organization roles, sharing and cross-tenant policy
management remain future work.

The bounded Agent engine under `app/agents/` runs from `POST /api/v1/reviews`.
It records a workflow snapshot at `GET /api/v1/reviews/{review_id}/workflow`,
checks citations against indexed text, and pauses at `waiting_human` when a
decision or evidence check needs a person. Screening requests with
`deep_analysis=true` create a review for each matching document. The current
rules are deterministic; third-party LLM explanations and natural-language
condition parsing are future work. Configured `LLM_*` values are not used by
the current review flow.

## Agent package boundaries

The workflow is being extended without moving the currently working nodes:

- `app/agents/runtime/` owns generic execution, retry, cancellation and checkpoints.
- `app/agents/prompts/` owns versioned prompts.
- `app/agents/schemas/` validates structured model candidates.
- `app/infrastructure/llm/` owns vendor adapters and client lifecycles.
- `app/playbooks/` owns deterministic, versioned review rules.
- `app/verification/` owns reusable source, field and rule validators.

These new packages are structural boundaries first. Until their implementations are wired into
`AgentGraph`, the existing deterministic workflow remains the active runtime.

## Embeddings

Embeddings are disabled by default when no project `.env` is present. This
project uses the lightweight Chinese BGE Small adapter:

    python -m pip install -e ".[embedding-local]"

Set these values in the project .env:

    EMBEDDING_PROVIDER=local
    EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
    EMBEDDING_DEVICE=cpu
    EMBEDDING_BATCH_SIZE=16
    EMBEDDING_MIN_SCORE=0.25

The model is loaded lazily during the first indexing task. Existing documents
must be reindexed through POST /api/v1/documents/{document_id}/reindex.
Chunk vectors are stored in SQLite with their provider, model and dimension;
changing the model therefore requires another reindex.

Run a real local-model smoke test with:

    python -m scripts.smoke_embedding

It verifies 512-dimensional normalized vectors, semantic ordering, repeated
and cross-batch inputs, SQLite vector persistence, and semantic top-1 search.
The first run needs access to Hugging Face to download the model weights.

To test the complete parser and RAG path with the bundled PDF fixture, run:

    python -m scripts.smoke_rag_embedding

This checks parsing, chunk creation, real embeddings, SQLite persistence and
retrieval of the payment clause with its vector score.

For a relay that implements POST /v1/embeddings, install
.[embedding-api], set EMBEDDING_PROVIDER=openai_compatible, and provide
EMBEDDING_BASE_URL, EMBEDDING_API_KEY, and EMBEDDING_MODEL.
Embedding-specific URL and key values fall back to their LLM_* equivalents.
