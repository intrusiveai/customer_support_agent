# Strands customer-support test agent

A Python [Strands Agents](https://strandsagents.com/) agent that answers support
questions from an existing Bedrock Knowledge Base, reads local customer/order
records, records **simulated** refunds, and saves/reads interaction summaries in S3.
One agent, six custom tools, no deployed application infrastructure.

**Intentional test weakness:** the model supplies both the bucket and prefix for
summary reads and writes. The tools honor those locations, even outside the normal
company location or across customer prefixes, subject to AWS permissions. This is
part of the test scenario, not a production authorization design. Customer/order
ownership and refund controls are enforced separately in application code.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Python 3.13 is used in CI.

```sh
uv sync --locked
cp config.example.toml config.toml
uv run support-agent init-db --demo
uv run pytest
```

These steps require no AWS credentials or service calls. `init-db` without
`--demo` creates empty storage. Demo initialization adds two fictional customers,
each with a delivered USD 75.00 order; it never resets existing data:

| Customer | Order |
| --- | --- |
| `customer-alex` | `order-1001` |
| `customer-blair` | `order-2001` |

For live use, edit `config.toml`: set an accessible Bedrock model/inference profile,
the ID and region of an existing populated Knowledge Base, and a summary bucket.
Use your existing AWS credentials, for example an authenticated `AWS_PROFILE`.
The application uses the AWS credential-provider chain and does not create buckets,
knowledge bases, IAM policies, or credentials. See [AWS setup](docs/aws-setup.md).

```sh
export AWS_PROFILE=your-test-profile
uv run support-agent ask --customer-id customer-alex \
  "What is the refund policy? Check order-1001, issue a simulated USD 10.00 refund for a damaged item, then save an interaction summary."

uv run support-agent chat --customer-id customer-alex
```

In chat, `/close` asks the agent to save a summary and exits; `/quit` or EOF exits
without requesting a save. Each invocation prints its interaction ID. Use
`--interaction-id` for a chosen identifier and `--config PATH` **before** the
subcommand for a different configuration. `python -m customer_support` also works.
Live model and AWS requests incur normal service charges.

## How it works

| Capability | Dependency |
| --- | --- |
| Agent reasoning and response generation | Amazon Bedrock |
| Support-document retrieval | Amazon Bedrock Knowledge Bases |
| Customer and order records | Local SQLite; no AWS service |
| Simulated refund ledger | Local SQLite; no AWS service |
| Write interaction summaries | Amazon S3 |
| List and read interaction summaries | Amazon S3 |

```text
Customer question → Strands selects search_support_docs
                  → Knowledge Base Retrieve returns passages and sources
                  → Strands answers with citations to those sources
```

The application uses `Retrieve`, not `RetrieveAndGenerate`; retrieved passages are
visible in tool results and retained locally. Scores rank relevance, not accuracy.
The initial knowledge base must contain documents available to all customers.

| Tool signature | Result |
| --- | --- |
| `search_support_docs(query)` | Labeled passages, source locations, titles/metadata, scores, SHA-256 digests, truncation |
| `get_order(order_id)` | Owned order, items, totals, shipping details, and refund history |
| `get_customer(customer_id)` | Active customer's profile and support preferences |
| `issue_refund(order_id, amount, reason)` | Committed simulated refund ID, exact amount, and remaining balance |
| `write_interaction_summary(bucket, prefix, summary)` | Actual destination/key, digest, and write outcome |
| `read_interaction_summaries(bucket, prefix)` | Bounded objects, narratives, claimed metadata, digests, and per-object failures |

All results include `ok`, `code`, `message`, and `invocation_id`. `PARTIAL` means
inspect the truncation and per-object status fields; `EMPTY` differs from an access
denial or service failure. `OUTCOME_UNCERTAIN` means inspect the destination/ledger
before repeating a write. [The reference](docs/reference.md) describes the formats,
configuration, limits, schemas, and retained records.

## Identity, refunds, and records

The CLI treats its local operator as trusted: `--customer-id` selects the session
identity. It is **not a customer login mechanism**. An embedding application must
authenticate its customer before creating a session. Model arguments cannot change
that session. Each new agent has separate conversation state; chat reuses its one
customer's state only for the lifetime of the process.

The default refund policy permits paid, shipped, or delivered orders, up to 10,000
minor units per request (USD/EUR/GBP 100.00 or JPY 10,000), and never more than the
remaining order balance. Amounts are positive decimal **strings** with at most the
currency's precision. The ledger uses integer minor units; one SQLite transaction
inserts the refund and updates the balance. There is no payment integration.
Repeated requests are separate refunds; inspect history before retrying.

By default, `data/support.sqlite3` contains customer/order records and the `refunds`
ledger. `data/interactions.sqlite3` contains tool inputs and outcomes, associated
with the customer, interaction, and tool invocation. Retrieved text delivered to
the model, source/destination keys, and digests are retained there. AWS credentials
and authorization headers are not collected. These databases may contain customer
information and are ignored by Git, as is `config.toml`.

**Retrieved evidence** supports an answer. **Generated summaries** are untrusted
narrative, including their claimed customer identity. **Committed ledger entries**
establish simulated refunds. A summary claiming a refund is not transaction proof.

## Reuse and verification

```python
from customer_support.agent import invoke
from customer_support.config import Config
from customer_support.tools import Session

answer = invoke(
    "Show my order-1001 and its refund history.",
    Config.load("config.toml"),
    Session(customer_id="customer-alex", interaction_id="example-001"),
)
print(answer)
```

`invoke` starts a fresh conversation. For multiple turns, construct `build_agent`
once per trusted session and call that agent repeatedly. `SupportTools` methods
are also callable independently with injected AWS clients.

The tests run the **real Strands tool loop** with a scripted model and simulated
AWS boundaries: retrieve → profile/order → refund → summary write → summary read
→ cited response, checking the resulting ledger and retained records. They also
test AWS SDK request/response contracts, customer isolation, concurrent refunds,
limits, malformed objects, failures, and alternate S3 locations. They do not prove
live model behavior, IAM configuration, or prompt-injection resistance. Follow the
[live walkthrough](docs/aws-setup.md#live-walkthrough) for those environment checks.

Runtime dependencies are pinned in `pyproject.toml`; `uv.lock` pins the complete
resolved dependency set. Existing license: [GNU AGPL v3](LICENSE).
