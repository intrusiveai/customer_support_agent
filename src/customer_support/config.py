"""Small, explicit TOML configuration; never stores AWS credentials."""

from dataclasses import dataclass, fields
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    model_id: str
    knowledge_base_id: str
    summary_bucket: str
    bedrock_region: str = "us-west-2"
    knowledge_base_region: str = "us-west-2"
    s3_region: str = "us-west-2"
    database_path: str = "data/support.sqlite3"
    interaction_record_path: str = "data/interactions.sqlite3"
    summary_read_prefix: str = "support/{customer_id}/"
    summary_write_prefix: str = "support/{customer_id}/"
    retrieval_results: int = 5
    retrieval_text_chars: int = 12000
    s3_list_limit: int = 10
    s3_download_bytes: int = 262144
    s3_object_bytes: int = 65536
    s3_returned_text_chars: int = 16000
    summary_max_chars: int = 8000
    connect_timeout_seconds: int = 5
    read_timeout_seconds: int = 30
    tool_timeout_seconds: int = 60
    model_max_tokens: int = 2048
    max_tool_calls: int = 24
    refund_eligible_statuses: tuple[str, ...] = ("paid", "shipped", "delivered")
    refund_max_minor: int = 10000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type is int and (type(value) is not int or value <= 0):
                raise ValueError(f"{field.name} must be a positive integer")
            if field.type is str and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field.name} must be a nonempty string")
        if self.retrieval_results > 100 or self.s3_list_limit > 1000:
            raise ValueError("retrieval_results must be <= 100; s3_list_limit <= 1000")
        if not self.refund_eligible_statuses or any(
            s not in {"paid", "shipped", "delivered"} for s in self.refund_eligible_statuses
        ):
            raise ValueError("refund_eligible_statuses must select paid, shipped, or delivered")
        if Path(self.database_path).resolve() == Path(self.interaction_record_path).resolve():
            raise ValueError("Database and interaction record paths must differ")
        for prefix in (self.summary_read_prefix, self.summary_write_prefix):
            # Only this literal placeholder is interpreted, not arbitrary formatting.
            if len(prefix.replace("{customer_id}", "x").encode()) > 900:
                raise ValueError("Normal summary prefixes must fit an S3 object key")

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        path = Path(path)
        with path.open("rb") as source:
            values = tomllib.load(source)
        unknown = values.keys() - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration settings: {', '.join(sorted(unknown))}")
        if "refund_eligible_statuses" in values:
            values["refund_eligible_statuses"] = tuple(values["refund_eligible_statuses"])
        # Storage paths are relative to the config file, independent of the caller's cwd.
        for name, default in (("database_path", "data/support.sqlite3"),
                              ("interaction_record_path", "data/interactions.sqlite3")):
            values[name] = str(path.resolve().parent / values.get(name, default))
        return cls(**values)
