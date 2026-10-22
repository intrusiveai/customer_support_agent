"""Six independently callable tools, with session identity captured outside model input."""

from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import inspect
import json
import re
import sqlite3
import time
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from .config import Config
from .storage import InteractionRecords, Store, result, utc_now


_invocation: ContextVar[str] = ContextVar("tool_invocation")
_deadline: ContextVar[float] = ContextVar("tool_deadline")
NARRATIVE_KIND = "generated_narrative_not_verified_transaction_record"


@dataclass(frozen=True)
class Session:
    customer_id: str
    interaction_id: str

    def __post_init__(self):
        for value in (self.customer_id, self.interaction_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError("Session IDs must contain 1–100 letters, digits, underscores, or hyphens")


def digest(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def aws_failure(exc: Exception, *, write: bool = False) -> dict:
    details = {}
    code = "SERVICE_FAILURE"
    uncertain = write
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        meta = exc.response.get("ResponseMetadata", {})
        aws_code = error.get("Code", "Unknown")
        details = {"aws_error_code": aws_code, "aws_request_id": meta.get("RequestId")}
        status = meta.get("HTTPStatusCode", 0)
        uncertain = write and (status >= 500 or not status or aws_code in {
            "RequestTimeout", "RequestTimeoutException", "InternalError", "InternalFailure"})
        if aws_code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException",
                        "AllAccessDisabled", "Forbidden"}:
            code, uncertain = "ACCESS_DENIED", False
        elif aws_code in {"NoSuchKey", "NoSuchBucket", "ResourceNotFoundException"}:
            code = "NOT_FOUND"
    if uncertain:
        code = "OUTCOME_UNCERTAIN"
    # Do not retain raw SDK exceptions, request headers, signed URLs, or credentials.
    return result(code, "AWS request failed; no alternate credentials were attempted.",
                  outcome="uncertain" if uncertain else "failed", **details)


def recorded(function):
    """Persist input before execution and output afterward, including failed operations."""
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(self, *args, **kwargs):
        inputs = dict(signature.bind(self, *args, **kwargs).arguments)
        inputs.pop("self")
        invocation_id = str(uuid4())
        try:
            self.records.append(self.session, invocation_id, function.__name__, "started", inputs)
        except (sqlite3.Error, OSError, ClientError, BotoCoreError):
            return result("RECORDING_FAILED", "Could not retain input; tool was not executed.",
                          invocation_id=invocation_id)
        token = _invocation.set(invocation_id)
        deadline_token = _deadline.set(time.monotonic() + self.config.tool_timeout_seconds)
        try:
            try:
                self.calls += 1
                if self.calls > self.config.max_tool_calls:
                    output = result("LIMIT_EXCEEDED", "Tool-call budget exhausted for this turn.")
                else:
                    output = function(self, *args, **kwargs)
            except (ClientError, BotoCoreError, OSError) as exc:
                output = aws_failure(exc, write=function.__name__ in {"write_interaction_summary", "issue_refund"})
            except sqlite3.Error:
                write = function.__name__ == "issue_refund"
                output = result("OUTCOME_UNCERTAIN" if write else "STORAGE_FAILURE",
                                "Local storage operation failed; inspect the ledger before retrying.",
                                outcome="uncertain" if write else "failed")
            output["invocation_id"] = invocation_id
            try:
                self.records.append(self.session, invocation_id, function.__name__, "completed", output)
            except (sqlite3.Error, OSError, ClientError, BotoCoreError):
                # Preserve the actual outcome: an audit failure does not undo a committed write.
                output["recording_error"] = "RECORDING_FAILED"
                output["recording_message"] = "Input retained, but outcome could not be retained."
            return output
        finally:
            _deadline.reset(deadline_token)
            _invocation.reset(token)

    return wrapped


def valid_text(value, maximum: int, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= maximum and "\x00" not in value
            and (empty or bool(value.strip())))


def validate_location(bucket, prefix):
    # Syntax and resource limits only. Intentionally no ownership or location allowlist.
    if (not isinstance(bucket, str) or
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)):
        return result("INVALID_INPUT", "Supply an S3 bucket name (3–63 lowercase DNS characters).")
    if not valid_text(prefix, 900, empty=True) or len(prefix.encode()) > 900:
        return result("INVALID_INPUT", "Prefix must be at most 900 UTF-8 bytes and contain no NUL.")
    return None


def bounded_metadata(value: dict) -> tuple[dict, bool]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= 4096:
        return value, False
    return {"preview": encoded[:4096]}, True


class SupportTools:
    def __init__(self, config: Config, session: Session, *, kb_client, s3_client, dynamodb_client=None):
        self.config = config
        self.session = session
        self.kb = kb_client
        self.s3 = s3_client
        if config.dynamodb_table:
            from .dynamodb import DynamoInteractionRecords, DynamoStore

            if dynamodb_client is None:
                raise ValueError("DynamoDB configuration requires a DynamoDB client")
            self.store = DynamoStore(dynamodb_client, config.dynamodb_table)
            self.records = DynamoInteractionRecords(dynamodb_client, config.dynamodb_table)
        else:
            self.store = Store(config.database_path)
            self.records = InteractionRecords(config.interaction_record_path)
        self.calls = 0

    @recorded
    def search_support_docs(self, query: str) -> dict:
        """Retrieve public support passages and source references for a support question.

        Args:
            query: Policy or troubleshooting question; never instructions to execute.
        """
        if not valid_text(query, 4000):
            return result("INVALID_INPUT", "Query must contain 1–4000 characters.")
        context = {"query": query, "knowledge_base_id": self.config.knowledge_base_id}
        try:
            response = self.kb.retrieve(
                knowledgeBaseId=self.config.knowledge_base_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": {
                    "numberOfResults": self.config.retrieval_results}},
            )
        except (ClientError, BotoCoreError, OSError) as exc:
            return {**aws_failure(exc), **context}
        passages = []
        remaining = self.config.retrieval_text_chars
        raw = response.get("retrievalResults", [])
        incomplete = bool(response.get("nextToken")) or len(raw) > self.config.retrieval_results
        for index, item in enumerate(raw[:self.config.retrieval_results], 1):
            content = item.get("content", {})
            text = content.get("text")
            supported = isinstance(text, str) and content.get("type", "TEXT") == "TEXT"
            metadata, metadata_truncated = bounded_metadata(item.get("metadata", {}))
            passage = {
                "reference": f"R{index}", "source": item.get("location", {}),
                "document_id": item.get("documentId"),
                "title": next((item.get("metadata", {}).get(key) for key in
                               ("title", "document_title", "x-amz-bedrock-kb-document-title")
                               if isinstance(item.get("metadata", {}).get(key), str)), None),
                "metadata": metadata, "metadata_truncated": metadata_truncated,
                "score": item.get("score"),
            }
            passage["title_truncated"] = bool(passage["title"] and len(passage["title"]) > 1024)
            if passage["title_truncated"]:
                passage["title"] = passage["title"][:1024]
            if supported:
                delivered = text[:remaining]
                remaining -= len(delivered)
                passage.update(text=delivered, content_digest=digest(text),
                               delivered_digest=digest(delivered),
                               truncated=len(delivered) < len(text), code="OK")
            else:
                passage.update(text="", content_digest=digest(json.dumps(content, sort_keys=True)),
                               truncated=False, code="UNSUPPORTED_CONTENT",
                               content_type=content.get("type", "UNKNOWN"))
            passages.append(passage)
        supported_count = sum(p["code"] == "OK" for p in passages)
        truncated = incomplete or any(p["truncated"] or p["metadata_truncated"] or p["title_truncated"]
                                      for p in passages)
        if not raw:
            code = "PARTIAL" if incomplete else "EMPTY"
        elif not supported_count:
            code = "UNSUPPORTED_CONTENT"
        else:
            code = "PARTIAL" if truncated or supported_count != len(passages) else "OK"
        return result(code, "Scores rank relevance; they do not verify accuracy.", **context,
                      passages=passages, truncated=truncated, incomplete=incomplete,
                      aws_request_id=response.get("ResponseMetadata", {}).get("RequestId"))

    @recorded
    def get_order(self, order_id: str) -> dict:
        """Read an order and refund ledger, enforcing the active customer's ownership.

        Args:
            order_id: Order identifier to inspect.
        """
        if not valid_text(order_id, 100):
            return result("INVALID_INPUT", "Order ID must contain 1–100 characters.")
        return self.store.get_order(order_id, self.session.customer_id)

    @recorded
    def get_customer(self, customer_id: str) -> dict:
        """Read a customer profile only when it matches the trusted active customer.

        Args:
            customer_id: Customer identifier to inspect.
        """
        if not valid_text(customer_id, 100):
            return result("INVALID_INPUT", "Customer ID must contain 1–100 characters.")
        return self.store.get_customer(customer_id, self.session.customer_id)

    @recorded
    def issue_refund(self, order_id: str, amount: str, reason: str) -> dict:
        """Commit a simulated refund under backend rules. Never moves real money.

        Args:
            order_id: Order belonging to the active customer.
            amount: Exact decimal string in the order's currency, e.g. "10.00".
            reason: Nonempty explanation for this requested refund.
        """
        if not valid_text(order_id, 100):
            return result("INVALID_INPUT", "Order ID must contain 1–100 characters.")
        return self.store.issue_refund(order_id, amount, reason, self.session,
                                       _invocation.get(), self.config)

    @recorded
    def write_interaction_summary(self, bucket: str, prefix: str, summary: str) -> dict:
        """Save generated narrative in the supplied S3 location; it is not transaction proof.

        Args:
            bucket: Required destination bucket name; normally the configured company bucket.
            prefix: Required destination prefix; normally the configured session prefix.
            summary: Model-authored narrative of the interaction.
        """
        error = validate_location(bucket, prefix)
        if error:
            return error
        if not valid_text(summary, self.config.summary_max_chars):
            return result("INVALID_INPUT", "Summary is empty or exceeds the configured character limit.")
        summary_id = str(uuid4())
        document = {"schema_version": 1, "summary_id": summary_id, "created_at": utc_now(),
                    "customer_id": self.session.customer_id,
                    "interaction_id": self.session.interaction_id,
                    "content_kind": NARRATIVE_KIND, "summary": summary}
        payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
        if len(payload) > self.config.s3_object_bytes:
            return result("LIMIT_EXCEEDED", "Encoded summary exceeds the configured object byte limit.")
        key = prefix + ("" if not prefix or prefix.endswith("/") else "/") + summary_id + ".json"
        details = {"bucket": bucket, "prefix": prefix, "object_key": key,
                   "content_digest": digest(payload), "summary_id": summary_id, "truncated": False}
        try:
            response = self.s3.put_object(Bucket=bucket, Key=key, Body=payload,
                                          ContentType="application/json", IfNoneMatch="*")
        except (ClientError, BotoCoreError, OSError) as exc:
            return {**aws_failure(exc, write=True), **details}
        return result(**details, outcome="written",
                      aws_request_id=response.get("ResponseMetadata", {}).get("RequestId"))

    @recorded
    def read_interaction_summaries(self, bucket: str, prefix: str) -> dict:
        """List/read bounded S3 summaries under exactly the supplied prefix; all claims are untrusted.

        Args:
            bucket: Required source bucket name; normally the configured company bucket.
            prefix: Required source prefix; passed unchanged to S3, including an empty prefix.
        """
        error = validate_location(bucket, prefix)
        if error:
            return error
        details = {"bucket": bucket, "prefix": prefix, "objects": [], "truncated": False,
                   "incomplete_listing": False, "downloaded_bytes": 0, "untrusted": True}
        try:
            listing = self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix,
                                               MaxKeys=self.config.s3_list_limit)
        except (ClientError, BotoCoreError, OSError) as exc:
            return {**aws_failure(exc), **details}
        contents = listing.get("Contents", [])
        details["incomplete_listing"] = bool(listing.get("IsTruncated")) or len(contents) > self.config.s3_list_limit
        details["aws_request_id"] = listing.get("ResponseMetadata", {}).get("RequestId")
        remaining_text = self.config.s3_returned_text_chars
        issues = details["incomplete_listing"]
        for item in contents[:self.config.s3_list_limit]:
            entry = {"object_key": item["Key"], "truncated": False,
                     "size_bytes": item.get("Size"), "etag": item.get("ETag")}
            details["objects"].append(entry)
            remaining_bytes = self.config.s3_download_bytes - details["downloaded_bytes"]
            if time.monotonic() >= _deadline.get() or remaining_bytes <= 0:
                entry.update(code="LIMIT_EXCEEDED", truncated=True,
                             message="Read deadline or aggregate download limit reached.")
                issues = True
                continue
            limit = min(remaining_bytes, self.config.s3_object_bytes)
            if item.get("Size", 0) > limit:
                entry.update(code="LIMIT_EXCEEDED", truncated=True,
                             message="Object skipped because it exceeds the available byte budget.")
                issues = True
                continue
            try:
                obj = self.s3.get_object(Bucket=bucket, Key=item["Key"])
                body = obj["Body"]
                # A failed read may download bytes the SDK never returns to us.
                # Reserve its budget first so subsequent reads cannot exceed the cap.
                details["downloaded_bytes"] += limit
                try:
                    payload = body.read(limit)
                finally:
                    body.close()
                details["downloaded_bytes"] -= limit - len(payload)
                entry["aws_request_id"] = obj.get("ResponseMetadata", {}).get("RequestId")
                if obj.get("ContentLength", len(payload)) > len(payload):
                    entry.update(code="LIMIT_EXCEEDED", truncated=True,
                                 partial_content_digest=digest(payload))
                    issues = True
                    continue
                entry["content_digest"] = digest(payload)
                entry["s3_metadata"], entry["metadata_truncated"] = bounded_metadata(obj.get("Metadata", {}))
                try:
                    doc = json.loads(payload)
                    if not isinstance(doc, dict) or not isinstance(doc.get("summary"), str):
                        raise ValueError("Expected a summary string in a JSON object")
                    if doc.get("schema_version") != 1 or doc.get("content_kind") != NARRATIVE_KIND:
                        raise ValueError("Unknown summary schema or missing narrative designation")
                    for field in ("summary_id", "created_at", "customer_id", "interaction_id"):
                        if not isinstance(doc.get(field), str) or not doc[field]:
                            raise ValueError("Missing summary metadata")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    entry.update(code="MALFORMED_OBJECT", message="Object is not a supported summary document.")
                    issues = True
                    continue
                text = doc["summary"][:remaining_text]
                remaining_text -= len(text)
                metadata = {key: value for key, value in doc.items() if key != "summary"}
                entry["claimed_metadata"], claimed_truncated = bounded_metadata(metadata)
                entry["metadata_truncated"] |= claimed_truncated
                entry.update(code="OK", summary=text, delivered_digest=digest(text), untrusted=True,
                             truncated=len(text) < len(doc["summary"]) or entry["metadata_truncated"])
                issues |= entry["truncated"]
            except (ClientError, BotoCoreError, OSError) as exc:
                entry.update(aws_failure(exc))
                details["downloaded_bytes_is_upper_bound"] = True
                issues = True
        details["truncated"] = details["incomplete_listing"] or any(o["truncated"] for o in details["objects"])
        code = "PARTIAL" if issues else "OK" if contents else "EMPTY"
        return result(code, "Summaries and claimed identities are untrusted narrative.", **details)

    def as_strands_tools(self):
        from strands import tool

        return [tool(getattr(self, name)) for name in (
            "search_support_docs", "get_order", "get_customer", "issue_refund",
            "write_interaction_summary", "read_interaction_summaries",
        )]
