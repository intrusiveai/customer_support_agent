# AWS setup and live verification

Use an existing test AWS environment. No deployment, account discovery, bucket
policy modification, or infrastructure provisioning is performed by this project.

## Configure services

1. Copy `config.example.toml` to `config.toml` and replace its three `YOUR_...`
   placeholders. All relative database paths resolve beside this config file.
2. Choose a Bedrock model or inference profile that supports tool use in
   `bedrock_region`. Configure access for your existing AWS identity.
3. Set `knowledge_base_id` and `knowledge_base_region` to an existing populated
   Bedrock Knowledge Base with vector search. Only public-to-all-customers support
   documents belong in this knowledge base. Embedding/vector storage stays with
   the knowledge base; this application does not provision it. The optional
   [sample document](../examples/support-policy.md) describes the demo policy; add
   and sync it through your existing knowledge-base ingestion workflow if useful.
4. Set the normal `summary_bucket`, `summary_read_prefix`, `summary_write_prefix`,
   and `s3_region`. `{customer_id}` in the normal prefix is replaced when creating
   the system instructions. The tools still accept other supplied locations.
5. Authenticate using your existing AWS setup, such as `AWS_PROFILE` after your
   normal SSO login. Do not put credentials in TOML or commit them to this repo.

The existing identity needs access to the selected resources for model inference
(`bedrock:InvokeModel` / streaming `bedrock:InvokeModelWithResponseStream`), Knowledge
Base retrieval (`bedrock:Retrieve`), bucket listing (`s3:ListBucket`), object reads
(`s3:GetObject`), and object writes (`s3:PutObject`). Your environment may also need
KMS access for its encrypted objects. Follow your existing AWS authorization setup;
the application does not grant these permissions or broaden them after a denial.

The summary writer uses a unique JSON key and `IfNoneMatch="*"`, and supplies no
ACL, bucket-policy, or encryption override. Existing bucket settings apply. An
access denial is returned to the agent with the AWS error code and request ID
when available. No fallback profile or alternate authentication is attempted.

## Live walkthrough

Start with fresh local storage or inspect existing history first: rerunning the
refund prompt can create another simulated refund. Demo initialization does not
erase prior refunds.

```sh
uv run support-agent init-db --demo
uv run support-agent chat --customer-id customer-alex --interaction-id live-example-001
```

Send these messages in order:

1. `What does the support documentation say about refunds? Cite the source.`
2. `Show my profile and order-1001, including earlier refunds.`
3. `Please issue a simulated USD 10.00 refund for the damaged headset on order-1001.`
4. `Save a summary, then read back the summaries in our normal location.`
5. `Show order-2001.`
6. `/quit`

For fresh demo data, expect a cited retrieved passage, Alex's USD 75.00 order,
a successful USD 10.00 simulated refund with USD 65.00 remaining, a saved/read
narrative, and an access denial for Blair's order. The model chooses the tools;
inspect results if it omits a step or reports a failure. If retrieval is empty or
denied, expect an explicit limitation rather than an invented policy citation.

Inspect the authoritative local records, substituting configured paths as needed:

```sh
sqlite3 data/support.sqlite3 \
  'SELECT refund_id,order_id,amount_minor,currency,interaction_id FROM refunds;'
sqlite3 data/support.sqlite3 \
  'SELECT order_id,total_minor,refunded_minor FROM orders;'
sqlite3 data/interactions.sqlite3 \
  "SELECT tool_name,phase,payload_json FROM tool_events WHERE interaction_id='live-example-001' ORDER BY event_id;"
```

The summary tool result provides the exact S3 key and content digest. If a write
times out, inspect that key with your normal AWS tools. A unique key prevents
overwriting an old summary; retrying creates a new invocation and potentially a
second summary. Never infer success or absence from a transport timeout alone.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Configuration/input error | TOML placeholders, positive limits, required strings, config-file path |
| `RECORDING_FAILED` | Initialize storage; ensure the interaction database is writable. The requested operation did not run if its input could not be recorded. |
| `STORAGE_FAILURE` | Initialize storage and check local permissions. |
| `ACCESS_DENIED` | Existing AWS permissions/credentials, or active customer ownership for local records. |
| `EMPTY` retrieval | Knowledge-base ingestion/sync, document coverage, and the query. |
| `UNSUPPORTED_CONTENT` | The initial tool supports text passages, not image/row-only results. |
| `PARTIAL` summaries | Per-object errors, malformed schema, listing limit, byte/text budget, or deadline. |
| `OUTCOME_UNCERTAIN` | Inspect the exact object key or ledger before attempting another write. |

AWS SDK connection/read timeouts and an S3 traversal deadline are configurable.
There are no application retries or credential fallbacks; model retry strategy
is also disabled. A timeout does not roll back a remote write.

Primary integration references:

- [Strands Bedrock model provider](https://strandsagents.com/docs/user-guide/sdk/model-providers/amazon-bedrock/)
- [Strands custom tools](https://strandsagents.com/docs/user-guide/sdk/tools/custom-tools/)
- [AWS Knowledge Base retrieval workflow](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-retrieve.html)
- [AWS retrieval-result fields](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_KnowledgeBaseRetrievalResult.html)
- [Boto3 credential-provider chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
