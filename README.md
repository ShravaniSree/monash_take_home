# Northstar accounts-payable agent

## Problem

- An accounts-payable processor receives an invoice and has to decide whether it can be posted.
- The decision has to follow current finance policy, match the invoice to the vendor, the purchase order, and the receipt, and catch duplicates, blocked vendors, tolerance breaches, and missing evidence.
- A model must not change the recommendation or the amounts.
- Nothing is paid until a person with enough authority approves it.
- This project does that locally.
- Policy allows five recommendations: `APPROVE_FOR_POSTING`, `HOLD_FOR_INFORMATION`, `REJECT_DUPLICATE`, `REJECT_INVALID`, and `ESCALATE_CONTROL_REVIEW`.

## Architecture

- Google ADK runs four agents in order.
- Only the narrative agent calls a model, and that agent has no tools.
- The other three are code.

![Accounts payable workflow. The invoice request goes to evidence_agent, which calls the vendor, purchase-order, and history tools against fixtures/tools JSON and searches policy in data/policy.sqlite. reconcile_agent decides, narrative_agent explains, and gate_agent posts once to data/ap_agent.sqlite.](docs/architecture.png)

| Piece | What it stores |
| --- | --- |
| `evidence_agent` | Calls the vendor, purchase-order, and invoice-history tools, then retrieves policy. |
| `reconcile_agent` | Applies the controls in code: duplicates, vendor status, three-way match, tolerance, segregation of duties, and authority. Writes `reason`. |
| `narrative_agent` | Explains the recommendation already stored in session state. It cannot call tools or change the outcome. The default provider is fake. |
| `gate_agent` | Waits for approval, then calls `submit_finance_decision` once. |
| Policy index | Markdown in `finance_rag_corpus/finance_rag_corpus/` is ingested into `data/policy.sqlite`. Search is SQLite FTS5, ranked in code so stale and untrusted documents cannot outrank current policy. |
| Sessions | ADK stores the waiting run in `data/adk_sessions.sqlite`. |
| Ledger | Approved posts and audit events go to `data/ap_agent.sqlite`. |

## RAG

- Policy search is lexical. There is no vector index.
- On startup, `build_service` opens `data/policy.sqlite` and ingests every markdown file under `finance_rag_corpus/finance_rag_corpus/`.
- A file is skipped when its SHA-256 hash matches the hash already stored for that document. Otherwise its old chunks are deleted and the file is indexed again.
- Each file's YAML front matter supplies `document_id`, `title`, `version`, `status`, `classification`, and `tags`.
- `effective_date`, `owner`, and `jurisdiction` are not stored.
- The body is split on `##` headings. One section becomes one chunk.
- The chunk id is `document_id`, the heading slug, and the section number, for example `FIN-POL-001#5-required-checks#4`.
- A file with no `##` heading is stored as a single chunk.

`PolicyIndex` keeps three tables:

| Table | What is stored |
| --- | --- |
| `documents` | `document_id`, `title`, `version`, `status`, `classification`, `tags`, `content_hash` |
| `chunks` | `chunk_id`, `document_id`, `title`, `version`, `section`, `status`, `classification`, `tags`, `body`, `ordinal` |
| `chunks_fts` | FTS5 index of `title`, `section`, and `body`, tokenized with the Porter stemmer. `chunk_id` is stored and not indexed. |

- A search uses SQLite `bm25` on that FTS table, then adjusts the score in code.
- Current text is boosted. Superseded or untrusted text is penalized.
- Superseded and adversarial documents stay in the index so they can be retrieved, but they cannot outrank current trusted policy.
- The processor role only receives classifications `internal` and `external-unverified`.
- Each hit returned to the evidence agent is a `RetrievedChunk`: `chunk_id`, `document_id`, `title`, `doc_type`, `version`, `section`, `status`, `trust`, `stale`, `classification`, `score`, and `text`.
- `trust` is trusted only for current policy.
- `stale` is true when `status` is `superseded`.
- `doc_type` is `policy`, `adversarial`, or `irrelevant`, taken from `status` and `tags`.

## Local setup

- Python 3.12 or newer is required.
- The packages are listed in [requirements.txt](requirements.txt): Google ADK, Pydantic, FastAPI, Uvicorn, and pytest.
- From the project root, create the environment, activate it, then install and start the server.

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m ap_agent.cli serve
```

- If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` once in that window, then run the activate command again.
- Command Prompt uses `.\.venv\Scripts\activate.bat` instead of `Activate.ps1`.

macOS (Terminal):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m ap_agent.cli serve
```

- The server listens on `127.0.0.1:8000`.
- The tool-call page is [http://127.0.0.1:8000/](http://127.0.0.1:8000/).
- `data/` is created on startup and holds the SQLite files.

## Model

- The narrative agent is the only agent that can call a model.
- It uses the local template when `LLM_PROVIDER` is `fake` or unset.
- That is the default in [.env.example](.env.example), and it is also what runs when there is no `.env`.
- The template repeats the recommendation the rules engine already stored. It does not call Gemini.
- To use Gemini, copy [.env.example](.env.example) to `.env` in the project root and set:

```
LLM_PROVIDER=gemini
ADK_MODEL=gemini-3.5-flash
GOOGLE_API_KEY=<your key>
```

- `.env` is gitignored.
- The CLI and the API read it on startup.
- Restart `python -m ap_agent.cli serve` after changing it.
- A variable already set in the shell is left as it is.

## API

With the server running, the generated docs are:

- Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- OpenAPI JSON: [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)

- A checked-in copy of the spec is [openapi/openapi.yaml](openapi/openapi.yaml).
- The running app is the current contract.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/runs` | Start a case. The body is an invoice request or a scenario file. |
| `GET` | `/runs/{run_id}` | Read the snapshot, including `reason` and the tool trace. |
| `POST` | `/runs/{run_id}/decision` | Approve or reject a waiting run. |
| `GET` | `/scenarios` | List fixture cases. |
| `POST` | `/evaluations` | Run every `FIN-*` fixture and compare the recommendation. |

## Mock tools and data

- Operational systems are not called.
- Each case id selects its rows from JSON:

| File | Tool |
| --- | --- |
| [fixtures/scenarios/](fixtures/scenarios/) | The invoice request for `FIN-001` through `FIN-016`. |
| [fixtures/tools/vendors.json](fixtures/tools/vendors.json) | `get_vendor_record` |
| [fixtures/tools/purchase_orders.json](fixtures/tools/purchase_orders.json) | `get_purchase_order`, including scripted timeouts |
| [fixtures/tools/invoice_history.json](fixtures/tools/invoice_history.json) | `check_invoice_history` |
| [fixtures/tools/rates.json](fixtures/tools/rates.json) | Corporate FX rates |

- `load_world` in `ap_agent/systems/fixtures.py` builds an in-memory `FinanceWorld` from the rows for that `case_id`.
- The scenario file does not embed the tool responses.
- Policy text is the markdown corpus, not one of these JSON files.
- Scenario descriptions are in [TEST_HELPER.md](TEST_HELPER.md).

## Limitations

- `submit_finance_decision` writes a simulated ledger row.
- It does not update the vendor, purchase-order, or invoice-history JSON, and it does not write to a bank or an ERP.
- Starting the same invoice again builds a fresh world from those files, so a case that was just approved still recommends approval.
- Duplicate detection only sees the history that was stored for that case id, such as `FIN-002`.
- A repeated approval of the same run returns the original ledger row.
- That replay is inside the run. It is not a second invoice.
- Retrieval is lexical FTS5, not a vector index.
- The default model is a template and does not call Gemini.
- A hold marked `RETRY_EVIDENCE` does not call the failed tool again by itself.
