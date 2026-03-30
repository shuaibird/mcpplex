#!/usr/bin/env python3
"""mcpplex — MCP log viewer TUI."""

import sys, re, json, argparse, threading, time
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from datetime import datetime
from textual.app import App, ComposeResult
from textual.content import Content
from textual.binding import Binding
from textual.widgets import (
    Header, Footer, DataTable, Input, Static, Label
)
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from rich.text import Text
from rich.syntax import Syntax

# ── parser ────────────────────────────────────────────────────────────────────

LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+'
    r'\[(?P<b1>[^\]]+)\]\s+'
    r'\[(?P<b2>[^\]]+)\]\s+'
    r'(?P<rest>.*)$'
)

_LEVEL_WORDS = {'error','fatal','warn','warning','info','debug','trace','verbose'}

def deep_parse(obj):
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith(('{','[')):
            try: return deep_parse(json.loads(s))
            except: pass
        return obj
    if isinstance(obj, list):  return [deep_parse(i) for i in obj]
    if isinstance(obj, dict):  return {k: deep_parse(v) for k,v in obj.items()}
    return obj

def _fmt_json_fragment(s):
    """Format a single JSON fragment with indentation."""
    out, indent, in_str, esc = [], 0, False, False
    for c in s:
        if esc:
            out.append(c); esc = False; continue
        if c == '\\' and in_str:
            out.append(c); esc = True; continue
        if c == '"':
            out.append(c); in_str = not in_str; continue
        if in_str:
            out.append(c); continue
        if c in '{[':
            indent += 1
            out.append(c + '\n' + '  ' * indent)
        elif c in '}]':
            indent = max(indent - 1, 0)
            out.append('\n' + '  ' * indent + c)
        elif c == ',':
            out.append(',\n' + '  ' * indent)
        elif c == ':':
            out.append(': ')
        elif c in ' \t':
            continue
        else:
            out.append(c)
    return ''.join(out)

_ORPHAN_LINE_RE = re.compile(r'^\s*"(?:[^"\\]|\\.)*"\s*,?\s*$')
_KV_LINE_RE = re.compile(r'^\s*"(?:[^"\\]|\\.)*"\s*:')

def fmt_raw_json(s):
    """Best-effort pretty-format for raw/truncated JSON strings."""
    s = _fmt_json_fragment(s)
    # Remove orphaned string values (no key) — broken fragments from truncation
    lines = s.split('\n')
    cleaned = [l for l in lines if not _ORPHAN_LINE_RE.match(l) or _KV_LINE_RE.match(l)]
    return '\n'.join(cleaned)

_json_decoder = json.JSONDecoder()

def extract_json(s):
    """Find the first valid JSON object/array in s, return (pre_text, parsed, post_text) or None."""
    for i, c in enumerate(s):
        if c not in '{[':
            continue
        try:
            obj, end = _json_decoder.raw_decode(s, i)
            return s[:i].strip(), obj, s[end:].strip()
        except (json.JSONDecodeError, ValueError):
            continue
    return None

def parse_line(raw: str) -> dict | None:
    m = LINE_RE.match(raw.strip())
    if not m: return None

    ts_raw = m.group('ts')
    b1, b2 = m.group('b1'), m.group('b2')
    # auto-detect [server] [level] vs [level] [server]
    if b1.lower() in _LEVEL_WORDS:
        server = b2
    else:
        server = b1
    rest = m.group('rest')

    # detect direction from "Message from client:" / "Message from server:" prefix
    direction = None
    dir_match = re.match(r'^Message from (client|server):\s*', rest)
    if dir_match:
        direction = dir_match.group(1)
        rest = rest[dir_match.end():]

    # strip trailing { metadata: ... } before JSON extraction
    rest_clean = re.sub(r'\s*\{\s*metadata:\s*\S+\s*\}\s*$', '', rest)

    text, payload = rest_clean, None
    has_trunc = re.search(r'\[\d+ chars? truncated\]', rest_clean)
    if has_trunc:
        # Try parsing from the first { or [ only (don't skip to inner objects)
        for i, c in enumerate(rest_clean):
            if c in '{[':
                json_part = rest_clean[i:]
                # Fix invalid JSON: escape control chars and invalid \escapes
                sanitized = re.sub(r'[\x00-\x1f]', lambda m: f'\\u{ord(m.group()):04x}', json_part)
                sanitized = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', sanitized)
                try:
                    obj, end = _json_decoder.raw_decode(sanitized, 0)
                    text = rest_clean[:i].strip()
                    payload = deep_parse(obj)
                except (json.JSONDecodeError, ValueError):
                    text = rest_clean[:i].strip()
                    payload = rest_clean[i:]
                break
    else:
        result = extract_json(rest_clean)
        if result:
            text = result[0]
            payload = deep_parse(result[1])
            # Append method name to text for JSON-RPC messages
            if isinstance(payload, dict):
                    prefix_parts = []
                    if 'id' in payload:
                        prefix_parts.append(f"#{payload['id']}")
                    if 'method' in payload:
                        prefix_parts.append(f"[{payload['method']}]")
                    if prefix_parts:
                        prefix = ' '.join(prefix_parts)
                        text = f"{prefix} {text}" if text else prefix

    try:
        dt = datetime.fromisoformat(ts_raw.replace('Z','+00:00'))
        ts_fmt = dt.astimezone().strftime('%H:%M:%S')
    except:
        ts_fmt = ts_raw

    # extract JSON-RPC id for pairing — try parsed payload first, then regex fallback
    msg_id = None
    if isinstance(payload, dict) and 'id' in payload:
        msg_id = payload['id']
    elif direction:
        # fallback: scan raw text for "id": <number> (handles truncated JSON)
        id_match = re.search(r'"id"\s*:\s*(\d+)', rest_clean)
        if id_match:
            msg_id = int(id_match.group(1))

    # extract method similarly
    method = None
    if isinstance(payload, dict) and 'method' in payload:
        method = payload['method']
    elif direction:
        method_match = re.search(r'"method"\s*:\s*"([^"]+)"', rest_clean)
        if method_match:
            method = method_match.group(1)

    return dict(
        ts=ts_fmt, ts_raw=ts_raw,
        server=server,
        direction=direction, msg_id=msg_id, method=method,
        text=text, payload=payload, raw=raw
    )

# ── detail modal ──────────────────────────────────────────────────────────────

def _render_payload(payload):
    """Return a Rich renderable for a payload value."""
    if payload is None:
        return Text('—')
    if isinstance(payload, str):
        return Syntax(fmt_raw_json(payload), "json", theme="monokai", line_numbers=False, word_wrap=True)
    payload_str = json.dumps(payload, indent=2, ensure_ascii=False)
    return Syntax(payload_str, "json", theme="monokai", line_numbers=False, word_wrap=True)

def _format_local_time(ts_raw):
    try:
        dt = datetime.fromisoformat(ts_raw.replace('Z','+00:00'))
        return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ts_raw

class DetailScreen(ModalScreen):
    BINDINGS = [("escape,q", "dismiss", "Close")]

    def __init__(self, entry: dict):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        e = self.entry

        with Vertical(id="detail-outer"):
            # ── paired view ──
            if e.get('_pair'):
                req, resp = e.get('request'), e.get('response')
                srv_color = _color_for_server(e['server'])
                if req and resp:
                    req_time = _format_local_time(req['ts_raw'])
                    resp_time = _format_local_time(resp['ts_raw'])
                    if req_time == resp_time:
                        header  = f"[bold]Time:[/]    {req_time}\n"
                    else:
                        header  = f"[bold]Time:[/]    {req_time} → {resp_time}\n"
                elif req:
                    header  = f"[bold]Time:[/]    {_format_local_time(req['ts_raw'])} → [dim]pending[/]\n"
                else:
                    header  = f"[bold]Time:[/]    {_format_local_time(resp['ts_raw'])}\n"
                header += f"[bold]Connector:[/]  [{srv_color}]{e['server']}[/]\n"
                header += f"[bold]Method:[/]  {e.get('method') or '—'}\n"
                yield Static(header, id="detail-meta")

                # Request section
                yield Static("[bold cyan]▶ Request[/]")
                if req:
                    yield Static(_render_payload(req['payload']), id="detail-req")
                else:
                    yield Static(Text("(no request captured)", style="dim"))

                yield Static("")  # spacer

                # Response section
                yield Static("[bold green]◀ Response[/]")
                if resp:
                    yield Static(_render_payload(resp['payload']), id="detail-resp")
                else:
                    yield Static(Text("(no response yet)", style="dim"))

            # ── single entry view ──
            else:
                local_time = _format_local_time(e['ts_raw'])
                srv_color = _color_for_server(e['server'])
                content  = f"[bold]Time:[/]       {local_time}\n"
                content += f"[bold]Connector:[/]  [{srv_color}]{e['server']}[/]\n"
                content += f"[bold]Event:[/]      {e['text'] or '—'}\n"
                yield Static(content, id="detail-meta")
                if e['payload'] is not None:
                    yield Static("[bold]Payload:[/]", id="payload-label")
                    yield Static(_render_payload(e['payload']), id="detail-json")

            yield Label("[dim]esc or q to close[/]", id="detail-hint")

    def on_click(self, event) -> None:
        try:
            widget = self.get_widget_at(event.screen_x, event.screen_y)[0]
            if hasattr(widget, 'id') and widget.id == "detail-hint":
                self.dismiss()
        except Exception:
            pass

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-outer {
        width: 90%;
        max-height: 85%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
        overflow-y: auto;
    }
    #detail-meta   { margin-bottom: 1; }
    #payload-label { color: $text-muted; margin-bottom: 0; }
    #detail-json   { overflow-x: auto; }
    #detail-req    { margin: 0 0 0 2; }
    #detail-resp   { margin: 0 0 0 2; }
    #detail-hint   { color: $text-muted; margin-top: 1; }
    """

# ── main app ──────────────────────────────────────────────────────────────────

_server_color_map: dict[str, str] = {}

def _assign_server_colors(entries: list):
    """Assign maximally-separated colors to all servers using golden ratio spacing."""
    import colorsys
    servers = sorted(set(e['server'] for e in entries))
    phi = (1 + 5 ** 0.5) / 2  # golden ratio
    for i, name in enumerate(servers):
        h = (i * phi) % 1.0
        r, g, b = colorsys.hls_to_rgb(h, 0.65, 0.7)
        _server_color_map[name] = f"rgb({int(r*255)},{int(g*255)},{int(b*255)})"

def _color_for_server(name: str) -> str:
    if name not in _server_color_map:
        import colorsys
        h = (len(_server_color_map) * ((1 + 5 ** 0.5) / 2)) % 1.0
        r, g, b = colorsys.hls_to_rgb(h, 0.65, 0.7)
        _server_color_map[name] = f"rgb({int(r*255)},{int(g*255)},{int(b*255)})"
    return _server_color_map[name]

def _title_from_path(path):
    """Derive a display title from the log file path.
    e.g. 'mcp-server-Read and Send iMessages.log' -> 'MCP Server Read and Send iMessages Log'
    """
    import os
    name = os.path.splitext(os.path.basename(path))[0]  # strip dir and .log
    name = name.replace('-', ' ')
    # Title-case: capitalize first letter of each word
    parts = name.split()
    titled = []
    for p in parts:
        if p.lower() == 'mcp':
            titled.append('MCP')
        elif p.lower() == 'server':
            titled.append('Server')
        else:
            titled.append(p[0].upper() + p[1:] if p else p)
    titled.append('Log')
    return ' '.join(titled)

class LogApp(App):
    TITLE = "mcpplex"
    BINDINGS = [
        Binding("q",      "quit",            "Quit"),
        Binding("enter",  "show_detail",     "Detail"),
        Binding("/",      "focus_search",    "Search"),
        Binding("escape", "close_search",    "Done", key_display="esc"),
        Binding("escape", "clear_all",       "Clear", key_display="esc"),
        Binding("f",      "toggle_follow_on",  "Follow"),
        Binding("f",      "toggle_follow_off", "Unfollow"),
    ]

    DEFAULT_CSS = """
    #toolbar {
        height: 3;
        background: $surface;
        border-bottom: solid $primary-darken-2;
        padding: 0 1;
        align: left middle;
    }
    #search {
        width: 30;
    }
    DataTable {
        height: 1fr;
    }
    """

    def __init__(self, entries, follow_path=None, follow_on=False):
        super().__init__()
        if follow_path:
            self.title = _title_from_path(follow_path)
        self.all_entries   = entries
        self.shown         = entries[:]
        self._search_term  = ''
        self._follow_path  = follow_path
        self._following    = follow_path is not None and follow_on
        self._file_offset  = 0  # bytes already read
        self._watcher_started = False
        self._time_sort    = 'desc'  # 'asc' | 'desc'

    def format_title(self, title: str, sub_title: str) -> Content:
        title_content = Content(title)
        if sub_title:
            return Content.assemble(
                title_content,
                (" — ", "dim"),
                Content(sub_title).stylize("bold green"),
            )
        return title_content

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Input(placeholder="/ search…", id="search")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    @property
    def _time_label(self):
        if self._time_sort == 'asc':
            return "Time ▲  "
        return "Time ▼  "

    def on_mount(self):
        t = self.query_one(DataTable)
        t.add_columns("  #", self._time_label, "Connector", "Event")
        self._rebuild_table()
        self.query_one("#toolbar").display = False
        t.focus()
        if self._following:
            import os
            self._file_offset = os.path.getsize(self._follow_path)
            self._watcher_started = True
            threading.Thread(target=self._watch_file, daemon=True).start()

    def _match_search(self, e, term):
        """Check if entry matches search term, checking both sides for pairs."""
        if not term:
            return True
        if e.get('_pair'):
            req, resp = e.get('request'), e.get('response')
            return ((req and term in req['raw'].lower()) or
                    (resp and term in resp['raw'].lower()))
        return term in e['raw'].lower()

    def _rebuild_table(self):
        term  = self._search_term.lower()
        # pair first, then search across both request and response
        paired = self._build_paired(list(self.all_entries))
        self.shown = [e for e in paired if self._match_search(e, term)]
        if self._time_sort == 'asc':
            self.shown.sort(key=lambda e: e['ts_raw'])
        elif self._time_sort == 'desc':
            self.shown.sort(key=lambda e: e['ts_raw'], reverse=True)
        t = self.query_one(DataTable)
        t.clear()
        for i, e in enumerate(self.shown):
            srv_color = _color_for_server(e['server'])
            if e.get('_pair'):
                # paired row: show method, status indicator
                method = e.get('method') or e.get('text') or ''
                status = "✓" if e.get('request') and e.get('response') else "→" if e.get('request') else "←"
                status_style = "green" if status == "✓" else "yellow"
                params_name = ''
                req = e.get('request')
                if req and isinstance(req.get('payload'), dict):
                    params = req['payload'].get('params')
                    if isinstance(params, dict) and 'name' in params:
                        params_name = f" {params['name']}"
                txt = f"{status} [{method}]{params_name}"[:80]
                t.add_row(
                    Text(str(i+1), style="dim"),
                    e['ts'],
                    Text(e['server'][:28], style=srv_color),
                    Text(txt, style=status_style if status != "✓" else srv_color),
                )
            else:
                txt = (e['text'] or ('(json)' if e['payload'] else ''))[:80]
                t.add_row(
                    Text(str(i+1), style="dim"),
                    e['ts'],
                    Text(e['server'][:28], style=srv_color),
                    Text(txt, style=srv_color),
                )
        self.sub_title = "● FOLLOWING" if self._following else ""

    def _build_paired(self, entries):
        """Combine client/server entries with the same (server, session, msg_id) into single rows.

        Sessions are delimited by 'Initializing server...' messages per server.
        IDs restart from 0 each session, so we must scope pairing within sessions.
        """
        from collections import OrderedDict
        # assign session ids per server
        session_counter: dict[str, int] = {}  # server -> current session id
        entry_sessions: list[int] = []
        for e in entries:
            srv = e['server']
            if srv not in session_counter:
                session_counter[srv] = 0
            # detect session boundary
            if e.get('direction') is None and 'Initializing server' in (e.get('text') or ''):
                session_counter[srv] += 1
            entry_sessions.append(session_counter[srv])

        pairs = OrderedDict()  # (server, session, msg_id) -> {request, response}
        unpaired = []
        for i, e in enumerate(entries):
            mid = e.get('msg_id')
            d = e.get('direction')
            if mid is not None and d in ('client', 'server'):
                key = (e['server'], entry_sessions[i], mid)
                if key not in pairs:
                    pairs[key] = {'request': None, 'response': None}
                if d == 'client':
                    pairs[key]['request'] = e
                else:
                    pairs[key]['response'] = e
            else:
                unpaired.append(e)

        result = []
        for (srv, _sess, mid), pair in pairs.items():
            req, resp = pair['request'], pair['response']
            primary = req or resp
            method = None
            if req:
                method = req.get('method')
            if not method and req and isinstance(req.get('payload'), dict):
                method = req['payload'].get('method')
            if not method and resp:
                method = resp.get('method')
            result.append({
                '_pair': True,
                'request': req,
                'response': resp,
                'server': srv,
                'msg_id': mid,
                'method': method,
                'ts': primary['ts'],
                'ts_raw': primary['ts_raw'],
                'text': method or primary.get('text') or '',
                'raw': primary['raw'],
            })
        result.extend(unpaired)
        return result

    def on_input_changed(self, event: Input.Changed):
        self._search_term = event.value
        self._rebuild_table()

    @property
    def _in_search(self):
        return self.query_one("#toolbar").display

    def check_action(self, action: str, parameters) -> bool | None:
        in_search = self._in_search
        if action == "focus_search":
            return not in_search
        if action == "close_search":
            return in_search
        if action == "clear_all":
            return not in_search
        if action == "toggle_follow_on":
            return not self._following
        if action == "toggle_follow_off":
            return self._following
        return True

    def action_focus_search(self):
        self.query_one("#toolbar").display = True
        inp = self.query_one("#search")
        inp.value = ''
        inp.focus()
        self.refresh_bindings()

    def action_close_search(self):
        inp = self.query_one("#search")
        inp.value = ''
        self.query_one("#toolbar").display = False
        self._search_term = ''
        self._rebuild_table()
        self.query_one(DataTable).focus()
        self.refresh_bindings()

    def action_clear_all(self):
        self.all_entries.clear()
        self._rebuild_table()

    def _do_toggle_follow(self):
        if not self._follow_path:
            return
        self._following = not self._following
        if self._following and not self._watcher_started:
            import os
            self._file_offset = os.path.getsize(self._follow_path)
            self._watcher_started = True
            threading.Thread(target=self._watch_file, daemon=True).start()
        self._rebuild_table()
        self.refresh_bindings()

    def action_toggle_follow_on(self):
        self._do_toggle_follow()

    def action_toggle_follow_off(self):
        self._do_toggle_follow()

    def _watch_file(self):
        """Background thread: poll file for new lines every 0.5s."""
        import os
        while True:
            time.sleep(0.5)
            if not self._following:
                continue
            try:
                size = os.path.getsize(self._follow_path)
                if size <= self._file_offset:
                    continue
                with open(self._follow_path, 'r') as f:
                    f.seek(self._file_offset)
                    new_text = f.read()
                    self._file_offset = f.tell()
                new_lines = [l for l in new_text.splitlines() if l.strip()]
                new_entries = [e for l in new_lines if (e := parse_line(l))]
                if new_entries:
                    self.call_from_thread(self._append_entries, new_entries)
            except Exception:
                pass

    def _append_entries(self, new_entries: list):
        self.all_entries.extend(new_entries)
        self._rebuild_table()
        # auto-scroll to newest entry
        t = self.query_one(DataTable)
        if self.shown:
            if self._time_sort == 'asc':
                t.move_cursor(row=len(self.shown) - 1)
            else:
                t.move_cursor(row=0)

    def action_show_detail(self):
        t   = self.query_one(DataTable)
        idx = t.cursor_row
        if 0 <= idx < len(self.shown):
            self.push_screen(DetailScreen(self.shown[idx]))

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected):
        # Column index 1 is the Time column
        if event.column_index == 1:
            self._time_sort = 'asc' if self._time_sort == 'desc' else 'desc'
            # Update the header label
            t = self.query_one(DataTable)
            col_key = t.columns[event.column_key]
            col_key.label = self._time_label
            self._rebuild_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self.action_show_detail()

# ── entrypoint ────────────────────────────────────────────────────────────────

def main():
    try:
        _version = _pkg_version('mcpplex')
    except PackageNotFoundError:
        _version = 'unknown'

    parser = argparse.ArgumentParser(description='mcpplex — MCP log TUI viewer')
    parser.add_argument('-v', '--version', action='version', version=f'mcpplex {_version}')
    parser.add_argument('file', nargs='?', help='.log file (or pipe via stdin)')
    parser.add_argument('-f', '--follow', action='store_true', help='Follow file for new lines (like tail -f)')
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            raw_lines = f.readlines()
        follow_path = args.file
    else:
        if args.follow:
            print("--follow requires a file path, not stdin.")
            sys.exit(1)
        if sys.stdin.isatty():
            parser.print_help()
            sys.exit(0)
        raw_lines   = sys.stdin.readlines()
        follow_path = None

    entries = [e for line in raw_lines if (e := parse_line(line))]
    if not entries:
        print("No parseable log lines found.")
        sys.exit(1)

    _assign_server_colors(entries)
    LogApp(entries, follow_path=follow_path, follow_on=args.follow).run()

if __name__ == '__main__':
    main()