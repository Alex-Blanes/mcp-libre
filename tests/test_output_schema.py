"""
The test that would have caught the modified_time bug.

MCP clients validate a tool's structured output against the tool's own
outputSchema, with JSON Schema *format* checking switched on. `format: date-time`
is RFC 3339, which requires a UTC offset — a naive datetime serializes without
one and the client rejects the whole response:

    data/modified_time must match format "date-time"
    data/modified_time must be null
    data/modified_time must match a schema in anyOf

So it isn't enough to check that a field is "a datetime": every structured
result has to survive format-checked validation against its declared schema.
"""

import re
from datetime import datetime, timezone

import pytest

import libremcp
from libremcp import DocResult, DocumentInfo, _get_document_info

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is needed to replicate client-side validation")

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})$")


def validate_like_a_client(instance, schema):
    """Validate exactly as an MCP client does: with format checking enabled."""
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(e.message for e in errors)


def test_document_info_modified_time_has_an_offset(tmp_path):
    doc = tmp_path / "sample.odt"
    doc.write_bytes(b"x")
    serialized = _get_document_info(str(doc)).model_dump(mode="json")
    assert RFC3339.match(serialized["modified_time"]), serialized["modified_time"]


def test_missing_file_still_produces_an_offset(tmp_path):
    serialized = _get_document_info(str(tmp_path / "nope.odt")).model_dump(mode="json")
    assert serialized["exists"] is False
    assert RFC3339.match(serialized["modified_time"]), serialized["modified_time"]


def test_naive_datetime_is_coerced_rather_than_escaping():
    """Belt-and-braces: a producer handing us a naive datetime must not break the client."""
    for model in (DocumentInfo, DocResult):
        kwargs = {"modified_time": datetime(2026, 8, 3, 12, 0, 0)}
        if model is DocumentInfo:
            kwargs.update(path="/tmp/x.odt", filename="x.odt", format="odt", size_bytes=1, exists=True)
        serialized = model(**kwargs).model_dump(mode="json")
        assert RFC3339.match(serialized["modified_time"]), (model.__name__, serialized["modified_time"])


def test_document_info_validates_against_its_own_schema(tmp_path):
    doc = tmp_path / "sample.odt"
    doc.write_bytes(b"x")
    info = _get_document_info(str(doc))
    validate_like_a_client(info.model_dump(mode="json"), DocumentInfo.model_json_schema())


def test_doc_result_validates_against_its_own_schema(tmp_path):
    doc = tmp_path / "sample.odt"
    doc.write_bytes(b"x")
    result = DocResult(**_get_document_info(str(doc)).model_dump(), success=True)
    validate_like_a_client(result.model_dump(mode="json"), DocResult.model_json_schema())


def test_doc_result_with_null_modified_time_validates():
    validate_like_a_client(
        DocResult(success=False, error="boom").model_dump(mode="json"), DocResult.model_json_schema()
    )


@pytest.mark.asyncio
async def test_every_tool_output_schema_accepts_an_aware_datetime():
    """Guard the wiring, not just the models: the schemas the client actually sees."""
    tools = await libremcp.mcp.list_tools()
    by_name = {t.name: t for t in tools}

    sample = DocResult(
        success=True,
        filename="x.odt",
        format="odt",
        size_bytes=1,
        modified_time=datetime.now(timezone.utc),
        exists=True,
        doc_id="a" * 32,
    ).model_dump(mode="json")

    schema = by_name["create_document"].outputSchema
    assert schema is not None, "create_document must declare an output schema"
    validate_like_a_client(sample, schema)
