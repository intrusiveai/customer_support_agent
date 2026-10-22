# Implementation reference

The implementation contract is [STRANDS_CUSTOMER_SUPPORT_AGENT.md](../STRANDS_CUSTOMER_SUPPORT_AGENT.md).
The application is separated into configuration, agent construction, tools, storage,
and CLI modules under `src/customer_support`. It has one Strands agent, sequential
tool execution, and no server or background service.

## Tool results and trust

Each independently callable tool returns a JSON-compatible dictionary:

```json
{
  "ok": true,
  "code": "OK",
  "message": "",
  "invocation_id": "generated-uuid",
  "outcome": "committed",
  "refund_id": "generated-uuid",
  "order_id": "order-1001",
  "amount": "10.00",
  "amount_minor": 1000,
  "currency": "USD",
  "refunded_minor": 1000,
  "remaining_minor": 6500,
  "remaining_balance": "65.00",
  "simulated": true
}
```

Only the common fields are universal; each tool adds its own data. `ok` is true
for `OK`, `EMPTY`, and `PARTIAL`; it does not certify that every requested item
was returned. Check `code` and per-item results. Application codes are:

| Code | Meaning |
| --- | --- |
| `OK`, `EMPTY`, `PARTIAL` | Complete result, no matches, or incomplete/mixed result |
| `INVALID_INPUT`, `INVALID_AMOUNT` | Argument or exact-money validation failed |
| `ACCESS_DENIED`, `NOT_FOUND` | Ownership/AWS denial, or missing resource |
| `INELIGIBLE_ORDER`, `POLICY_LIMIT`, `INSUFFICIENT_BALANCE` | Refund rejected without changing the ledger |
| `UNSUPPORTED_CONTENT`, `MALFORMED_OBJECT` | Non-text retrieved passage, or unsupported summary JSON |
| `LIMIT_EXCEEDED` | Call, byte, size, or traversal-time budget exhausted |
| `SERVICE_FAILURE`, `STORAGE_FAILURE` | AWS/local read failed |
| `OUTCOME_UNCERTAIN` | A write failed without a safely confirmed outcome |
| `RECORDING_FAILED` | Input could not be retained, so operation was not attempted |

AWS failures include `aws_error_code` and `aws_request_id` where available; raw
SDK requests, headers, exception text, and credentials are not retained. If the
operation completes but its outcome cannot be recorded, its actual result remains
intact and includes `recording_error="RECORDING_FAILED"`.

### Retrieval

The configured knowledge-base ID is never a tool argument. The response includes
the query, knowledge-base ID, passages, `truncated`, and `incomplete`. Each passage
has a response-local label (`R1`, `R2`, ...), text, source location, available title,
metadata, score, and digest. Labels restart with each retrieval, so the agent is
instructed to cite the actual location as well as the label.

`content_digest` is SHA-256 of the original passage's UTF-8 text;
`delivered_digest` hashes the text actually returned after truncation. Unsupported
content is identified and hashed as sorted JSON without returning binary content.
Source locations remain attached even when the text budget is exhausted. A
`nextToken` signals an incomplete retrieval; this bounded tool does not fetch
additional pages. Metadata has a fixed 4,096-character JSON preview limit, marked
with `metadata_truncated` when applied.
Titles have a 1,024-character limit with a separate `title_truncated` flag.

### S3 summaries

`bucket` and `prefix` are required model arguments to both summary tools. Empty
prefixes are allowed. Reads pass the prefix unchanged to `ListObjectsV2`.
Writes append a separator if necessary and a random UUID `.json` filename beneath
the supplied prefix. Prefix syntax/size validation does not normalize paths,
restrict accounts, compare against the normal bucket, or append a customer ID.

A saved JSON object contains `schema_version=1`, a generated `summary_id`, UTC
`created_at`, trusted session `customer_id` and `interaction_id`, model-authored
`summary`, and `content_kind="generated_narrative_not_verified_transaction_record"`.
The returned digest hashes the exact UTF-8 JSON bytes uploaded.

Reads list one bounded page, then fetch objects within the aggregate/per-object
byte budgets. They return keys, digests, summary text, claimed document metadata,
available S3 metadata, and individual codes. Oversized objects are skipped and
explicitly marked; invalid UTF-8/JSON/schema is `MALFORMED_OBJECT`. An object that
grows after listing may be returned as a truncated byte read with a
`partial_content_digest`. `content_digest` on a complete object hashes its exact
bytes; `delivered_digest` hashes only the delivered summary text. Truncation and
per-object failures produce `PARTIAL`, preserving successful items.
After a failed stream read, `downloaded_bytes_is_upper_bound` indicates that the
reserved byte budget was conservatively counted even if fewer bytes arrived.

Listings are not a "most recent summaries" query: the bounded page follows S3's
listing order. `incomplete_listing` means additional keys may exist. The tool
never automatically broadens or narrows the requested prefix. All parsed narrative
and identity claims remain untrusted, even if the object matches the schema.

## Configuration

`config.example.toml` lists every setting. Unknown keys and invalid limits are
rejected. AWS credentials are deliberately absent. Relative local paths resolve
against the TOML file's directory.

| Settings | Default / purpose |
| --- | --- |
| `model_id`, `knowledge_base_id`, `summary_bucket` | Required environment-specific values |
| `bedrock_region`, `knowledge_base_region`, `s3_region` | `us-west-2`, independently configurable |
| `database_path`, `interaction_record_path` | `data/support.sqlite3`, `data/interactions.sqlite3` |
| `summary_read_prefix`, `summary_write_prefix` | `support/{customer_id}/`; system-prompt guidance only |
| `retrieval_results`, `retrieval_text_chars` | 5 passages (maximum 100), 12,000 combined passage characters |
| `s3_list_limit` | 10 objects in one page (maximum 1,000) |
| `s3_download_bytes`, `s3_object_bytes` | 262,144 aggregate read bytes; 65,536 per object and per encoded write |
| `s3_returned_text_chars`, `summary_max_chars` | 16,000 combined read narrative characters; 8,000 per write |
| `connect_timeout_seconds`, `read_timeout_seconds` | 5-second connection timeout; 30-second socket read timeout |
| `tool_timeout_seconds` | 60-second S3 traversal budget, checked before each object request |
| `model_max_tokens`, `max_tool_calls` | 2,048 output tokens per model call; 24 tool calls per turn |
| `refund_eligible_statuses`, `refund_max_minor` | Paid/shipped/delivered; 10,000 minor units per refund |

Timeouts are socket and traversal budgets, not process termination or a strict
wall-clock deadline for an entire agent conversation. An in-flight request can
outlast the traversal budget until its socket timeout. No write is killed and
reported as rolled back. Per-turn tool limits stop repeated tool loops; independent
`SupportTools` instances count calls over their own lifetime. Query/ID/reason sizes
also have small fixed bounds. These limits do not bound the growth of local
databases; retention is the local operator's responsibility.

## Local schemas and transaction behavior

`init-db` creates these tables if absent. It does not migrate arbitrary older
schemas or overwrite existing records. `--demo` inserts fictional records only
when their IDs do not already exist.

| Database/table | Columns |
| --- | --- |
| Support / `customers` | `customer_id` primary key, `name`, `email`, `phone`, `address_json`, `preferences_json` |
| Support / `orders` | `order_id` primary key, `customer_id` foreign key, `items_json`, `total_minor`, `currency`, `status`, `refunded_minor` |
| Support / `refunds` | `refund_id` primary key, `order_id`, `amount_minor`, `currency`, `reason`, `customer_id`, `interaction_id`, unique `invocation_id`, `created_at` |
| Interaction / `tool_events` | `event_id` primary key, `invocation_id`, `customer_id`, `interaction_id`, `tool_name`, `phase`, `created_at`, `payload_json` |

Addresses, preferences, and items are JSON stored as SQLite text. Demo item fields
are SKU, name, quantity, and unit price in minor units. Only defined parameterized
queries are exposed; the model has no SQL tool. USD/EUR/GBP use two decimal places;
JPY uses zero. Unsupported currencies are excluded by the database constraint.
The explicit order total is authoritative; the test app does not calculate tax,
shipping, or discounts from item lines.

Refunds acquire a SQLite write transaction before reading the balance, checking
ownership, policy, and amount, then inserting a ledger record and updating the
balance. Both changes commit together. Exceptions roll back both. Concurrent
refund requests cannot spend the same remaining balance. SQLite failures are
conservatively reported as uncertain; verify the invocation ID in the ledger.

The default policy has no time window. A refund amount must be a positive plain
decimal string with no exponent, whitespace, or extra fractional places. The
configured limit is per operation; cumulative refunds are capped by the order
total. The model is instructed not to split requests to evade a limit. A new tool
call is a new request, not a business-level idempotency key, so callers must inspect
the ledger before repeating uncertain requests.

Each tool first commits a `started` record with inputs, then a `completed` record
with its returned outcome. A missing completion after a crash is not evidence that
a write did not happen. Refunds carry the same invocation ID in their transaction.
The records include delivered retrieved passages and digests, summary destinations
and object keys, truncation indicators, and refund outcomes. Prompt/answer transcripts
are not persisted; conversational memory stays in the individual agent instance.

Local databases are ordinary files and are not tamper-proof audit storage. Keep
them outside public commits and apply appropriate local access and retention rules.
