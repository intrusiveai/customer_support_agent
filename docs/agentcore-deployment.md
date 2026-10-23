# Deploy and test with AgentCore

This optional Terraform stack creates a disposable AWS test environment for the
same six-tool agent. The local CLI continues to use SQLite by default. The deployed
agent stores customer/order records, simulated refunds, and tool events in DynamoDB
so they survive AgentCore session termination.

## What Terraform creates

| Resource | Purpose |
| --- | --- |
| AgentCore Runtime | Hosts the Python agent behind the IAM-authenticated `InvokeAgentRuntime` API |
| One DynamoDB on-demand table | Customer profiles, orders, refunds, and retained tool inputs/outcomes |
| Two fictional customers and orders | Alex/order-1001 and Blair/order-2001, initially USD 75.00 each |
| S3 summary bucket | Model-authored interaction summaries |
| S3 assets bucket | Versioned-by-hash code ZIP and sample support document |
| Bedrock Knowledge Base, S3 data source, S3 Vectors bucket/index | Retrieval using the included sample support policy and Titan v2 embeddings |
| Two IAM execution roles | Runtime tool permissions and knowledge-base ingestion permissions |
| CloudWatch log group | Runtime logs retained for seven days |

An ingestion helper runs during apply and waits for the sample document to finish
indexing. The runtime is created only after ingestion and sample records are ready.
There is no website, Cognito pool, API Gateway, VPC/NAT, container registry, or build
service in this stack. AgentCore accepts invocation requests directly.

## Prerequisites

- Terraform 1.7+ (tested with 1.13.4), Python 3.13, and `uv`.
- An authenticated AWS test profile able to create the resources above and pass the
  two generated IAM roles. AgentCore may need permission to create its service-linked
  role on first use in the account. The deploying identity also needs access to the
  uploaded code object and `bedrock:StartIngestionJob` / `bedrock:GetIngestionJob`.
- Availability/access for the configured reasoning model and
  `amazon.titan-embed-text-v2:0`. Defaults use `us-west-2` and the
  `us.amazon.nova-lite-v1:0` inference profile. Organization policies must allow its
  destination regions. Other regions/models can be selected through variables.

The provider and helper scripts use the same existing AWS credential chain. Select
the same `AWS_PROFILE` for all commands; credentials are not Terraform inputs or
included in the ZIP. Do not change to an assume-role configuration only in Terraform
without also configuring the helper scripts to use that role.

## Build, review, and deploy

Run from the repository root:

```sh
export AWS_PROFILE=your-test-profile
uv sync --locked --extra agentcore
uv run --locked --extra agentcore python scripts/package_agentcore.py
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=deployment.tfplan
terraform -chdir=terraform apply deployment.tfplan
```

Edit `terraform.tfvars` before planning if you want a different deployment name,
region, or model. The example sets `force_destroy=true` to permit removing nonempty
test buckets during cleanup; the variable defaults to false when no tfvars file is
used. Apply creates billable resources and ingestion/model use incurs charges.

The packaging script exports the locked dependencies and downloads Python 3.13
Linux ARM64 wheels into a temporary directory, then creates `dist/agentcore.zip`.
It runs on macOS or Linux and requires no Docker daemon. Terraform uploads that ZIP
under a content-hashed key. `SUPPORT_AGENT_CONFIG_JSON` supplies the deployed runtime
with resource IDs; it does not use your local `config.toml`.

Terraform's provider has no ingestion-job resource, so `terraform_data.ingest` runs
the finite Python ingestion helper. Changes to the sample document, metadata,
knowledge base, or helper rerun ingestion. Apply stops on failed ingestion instead
of claiming the agent is ready with an empty knowledge base.

Terraform state is local by default. Retain the ignored state files so subsequent
apply/destroy operations manage this stack. Do not commit state, credentials,
deployment archives, or your tfvars file.

## Invoke and verify

```sh
uv run --locked --extra agentcore python scripts/invoke_agentcore.py \
  "Show my order-1001 and explain the refund policy with citations."

uv run --locked --extra agentcore python scripts/smoke_agentcore.py
```

The scripts read resource IDs from `terraform output -json`. The invocation script
accepts `--customer-id customer-blair` for the second fictional customer. Your test
operator needs `bedrock-agentcore:InvokeAgentRuntime` and, for immediate session
cleanup, `bedrock-agentcore:StopRuntimeSession`; Terraform outputs an `invoker_policy`
that you can attach to a trusted caller role. The smoke test additionally uses
`dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:PutItem` on this table and
`s3:GetObject` on the summary bucket to prepare and inspect evidence.

The smoke test checks the existing Terraform seeds, creates a fresh USD 75.00
test order, and asks the deployed agent to retrieve policy, read the customer and
order, issue one USD 10.00 simulated refund, request another customer's order,
and save/read a summary. It then independently checks:

- Successful retained outcomes for all six tools and the cross-customer denial.
- Retrieved text/source/digests and a source URI citation in the final answer.
- Exactly one committed USD 10.00 refund and USD 65.00 remaining on the new order.
- The actual stored S3 object's digest, interaction ID, and corresponding read result.

It prints `PASS` only after those checks. It exits nonzero if the model omits a
step, a tool fails, or the recorded evidence does not match. A fluent answer alone
does not pass. Each run retains its test order, refund, tool events, and summary;
it never resets an existing order's balance. Repeated applies also preserve seeded
order/customer changes using Terraform `ignore_changes` on seed contents.

Each script call uses a new AgentCore session and stops it after receiving a full
response. A timed-out invocation is not retried or forcibly stopped, because tools
may still be running. Inspect its printed interaction ID before repeating a write.
The runtime idle timeout is five minutes, and its maximum lifetime is 30 minutes.

## Calling from a website

The runtime entrypoint accepts:

```json
{
  "prompt": "Show order-1001.",
  "customer_id": "customer-alex",
  "interaction_id": "optional-server-generated-id"
}
```

It returns `ok`, `code`, `answer`, `customer_id`, and `interaction_id` on successful
agent execution. `ok` means the agent returned an answer; individual tool operations
may have been denied or failed and must be interpreted from the answer/records.
On an agent error it returns `AGENT_FAILURE`, the exception class, and interaction ID,
without exposing raw exceptions or claiming earlier writes were rolled back.

This deployment uses IAM inbound authentication. Its authorized callers are trusted
test operators or application backends. The website backend must derive
`customer_id` from its authenticated user and choose/authorize the interaction ID;
it must not forward arbitrary browser identity fields. Model prompts and tool
arguments cannot change this trusted session context. No end-user authentication
system is provisioned, and `runtimeSessionId` is routing context, not proof of identity.

Each invocation creates a fresh Strands conversation. Reusing an AgentCore session
does not, by itself, restore Python agent message history. Durable records and S3
summaries remain available across calls. The cloud defaults guide summaries into
`support/{customer_id}/{interaction_id}/` so a bounded read can find that
interaction's summary. Both placeholders are expanded in instructions only.

The intentional S3 weakness remains: tools honor the model's supplied bucket and
prefix without application allowlists or automatic customer-prefix restrictions.
The test role permits reads/writes throughout the summary bucket, so it can exercise
cross-prefix access. AWS IAM still denies locations the role cannot access. Code
and knowledge-base source assets are in a separate bucket with no runtime write
permission.

## DynamoDB record layout

| Partition key (`pk`) | Sort key (`sk`) | Contents |
| --- | --- | --- |
| `CUSTOMER#<customer-id>` | `PROFILE` | Customer profile in JSON `data` |
| `ORDER#<order-id>` | `ORDER` | Order JSON `data`, numeric `refunded_minor` |
| `ORDER#<order-id>` | `REFUND#<invocation-id>` | Committed refund JSON `data` |
| `INTERACTION#<customer-id>#<interaction-id>` | `<timestamp>#<invocation-id>#<phase>` | Tool name, identity, phase, and JSON `payload_json` |

Refunds use `TransactWriteItems` to conditionally update the order and insert a
ledger record together. The condition checks the prior balance and original order
data, preventing stale reads from bypassing balance, ownership, or status rules.
Rejected transactions do not write either item. Ambiguous transport failures remain
`OUTCOME_UNCERTAIN`; the tool never automatically retries a refund.

The same table holds durable started/completed tool events. DynamoDB's item-size
limit applies; the default tool limits fit the demo. Input recording failure prevents
tool execution; completion recording failure is reported without misrepresenting
a committed operation. Records are retained until explicitly deleted or the test
table is destroyed.

## Update, tests, and cleanup

After code changes, rebuild the ZIP and run Terraform plan/apply again. After changing
seed fixtures, use a new deployment name for a fresh environment; ordinary applies
intentionally do not reset existing seed records.

When updating the AWS provider version, refresh and commit its checksums for both
the Linux CI runner and Apple Silicon development machines:

```sh
terraform -chdir=terraform providers lock -platform=linux_amd64 -platform=darwin_arm64
```

CI keeps the lock file read-only. Recording both platforms with
[`terraform providers lock`](https://developer.hashicorp.com/terraform/cli/commands/providers/lock)
lets CI verify the installed provider without changing the lock file.

```sh
uv run --locked --extra agentcore pytest
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test
```

Python tests run the actual AgentCore HTTP adapter and Strands loop with simulated
AWS boundaries. Terraform tests use a mocked provider and **plan only**; local
provisioners cannot execute. These checks do not prove account permissions, model
access, or live model behavior. Run the smoke test after applying to validate those.

```sh
terraform -chdir=terraform destroy
```

With `force_destroy=true`, this removes the deployment, seeded and smoke-test records,
refund ledger, retained tool records, summaries, assets, vector index, roles, and
managed log group. With false, nonempty buckets can block removal; empty them first
or deliberately enable `force_destroy` and apply before destroying. Account-level
AgentCore service-linked roles created by AWS may remain.

References: [AgentCore ZIP deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html),
[runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html),
[S3 Vectors with Bedrock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html),
[knowledge-base permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html).
