import pytest
from mcpplex import deep_parse, extract_json, fmt_raw_json, parse_line, _title_from_path


# ── deep_parse ────────────────────────────────────────────────────────────────

def test_deep_parse_plain_string():
    assert deep_parse("hello") == "hello"

def test_deep_parse_json_string():
    assert deep_parse('{"a": 1}') == {"a": 1}

def test_deep_parse_nested_json_string():
    inner = '{"b": 2}'
    assert deep_parse({"a": inner}) == {"a": {"b": 2}}

def test_deep_parse_list():
    assert deep_parse(['{"x": 1}', "plain"]) == [{"x": 1}, "plain"]

def test_deep_parse_non_json_string_starting_with_brace():
    # invalid JSON — should return as-is
    assert deep_parse("{not json}") == "{not json}"

def test_deep_parse_passthrough():
    assert deep_parse(42) == 42
    assert deep_parse(None) is None


# ── extract_json ──────────────────────────────────────────────────────────────

def test_extract_json_object():
    pre, obj, post = extract_json('prefix {"key": "val"} suffix')
    assert pre == "prefix"
    assert obj == {"key": "val"}
    assert post == "suffix"

def test_extract_json_array():
    pre, obj, post = extract_json('[1,2,3]')
    assert pre == ""
    assert obj == [1, 2, 3]
    assert post == ""

def test_extract_json_no_json():
    assert extract_json("no json here") is None

def test_extract_json_empty():
    assert extract_json("") is None


# ── fmt_raw_json ──────────────────────────────────────────────────────────────

def test_fmt_raw_json_object():
    result = fmt_raw_json('{"a":1}')
    assert '"a"' in result
    assert "1" in result
    # should be multi-line
    assert "\n" in result

def test_fmt_raw_json_nested():
    result = fmt_raw_json('{"a":{"b":2}}')
    assert '"b"' in result

def test_fmt_raw_json_string_values_preserved():
    result = fmt_raw_json('{"key":"hello world"}')
    assert '"hello world"' in result


# ── parse_line ────────────────────────────────────────────────────────────────

SAMPLE_LINE = (
    '2024-01-15T10:30:00.000Z [myserver] [info] '
    'Message from client: {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
)

def test_parse_line_basic():
    entry = parse_line(SAMPLE_LINE)
    assert entry is not None
    assert entry["server"] == "myserver"
    assert entry["direction"] == "client"
    assert entry["msg_id"] == 1
    assert entry["method"] == "tools/list"

def test_parse_line_timestamp_format():
    entry = parse_line(SAMPLE_LINE)
    # ts should be HH:MM:SS
    assert len(entry["ts"]) == 8
    assert entry["ts"].count(":") == 2

def test_parse_line_invalid_returns_none():
    assert parse_line("not a valid log line") is None
    assert parse_line("") is None

def test_parse_line_server_direction_detection():
    # [level] [server] order
    line = (
        '2024-01-15T10:30:00.000Z [info] [myserver] '
        'Message from server: {"jsonrpc":"2.0","id":2,"result":{}}'
    )
    entry = parse_line(line)
    assert entry is not None
    assert entry["server"] == "myserver"
    assert entry["direction"] == "server"
    assert entry["msg_id"] == 2

def test_parse_line_no_json_payload():
    line = '2024-01-15T10:30:00.000Z [myserver] [info] Initializing server...'
    entry = parse_line(line)
    assert entry is not None
    assert entry["payload"] is None
    assert entry["direction"] is None

def test_parse_line_payload_parsed():
    entry = parse_line(SAMPLE_LINE)
    assert isinstance(entry["payload"], dict)
    assert entry["payload"]["method"] == "tools/list"


# ── _title_from_path ──────────────────────────────────────────────────────────

def test_title_from_path_basic():
    assert _title_from_path("/logs/mcp-server-foo.log") == "MCP Server Foo Log"

def test_title_from_path_multi_word():
    result = _title_from_path("/logs/mcp-server-Read and Send iMessages.log")
    assert result.startswith("MCP Server")
    assert result.endswith("Log")

def test_title_from_path_no_directory():
    assert _title_from_path("myserver.log") == "Myserver Log"
