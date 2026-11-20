# Non-Agent MVP status

The following implementation is now available behind the existing `/api/v1`
contract:

| Area | Implemented behavior |
| --- | --- |
| Documents | multipart upload, extension/MIME/size validation, SHA-256 deduplication, local object storage, SQLite metadata and versions |
| Parsing | PDF through PyMuPDF and DOCX through python-docx; page count, parser warnings and extracted text are persisted |
| Chunking | Page-aware, clause-title-aware chunks with overlap, approximate token budgets, stable global character offsets and per-chunk hashes |
| Tasks | In-process background executor, durable task state, event history and SSE polling |
| Screening | Typed amount, duration and risk comparisons over extracted facts |
| Evidence Q&A | Scope-limited keyword retrieval with citations and an explicit insufficient-evidence state |
| Review | Deterministic contract rules, findings, citations, optimistic human decisions and version diff |
| Reports | JSON, Markdown and CSV export |
| Authentication | Registration, login, current-user recovery, configurable required auth and owner-scoped resources |
| Agent engine | Fixed P0 plans, serializable state, bounded retries, deterministic extraction/risk nodes, Citation verification and reports |
| Web UI | React/Vite upload, document list, task progress, screening, findings, evidence and Q&A panels |

The Agent engine is currently a service-layer workflow and is not exposed as a
new HTTP contract. Model-backed explanations, OCR and a separately persisted
long-running Agent API remain follow-up work. The existing HTTP routes can
continue consuming persisted text, facts, citations and task events without
changing the frontend contract.
