import http.server
import socketserver
import json
import sublime
import uuid
import queue
import difflib


class MCPServerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.log_message("GET request to %s", self.path)
        if self.path.startswith("/mcp"):
            self.handle_sse()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_sse(self):
        auth_header = self.headers.get("Authorization")
        expected_token = self.server.auth_token
        self.log_message("Handling SSE. Auth: %s", auth_header)

        if not auth_header or (
            auth_header != expected_token and auth_header != "Bearer " + expected_token
        ):
            self.log_message("Auth failed")
            self.send_response(401)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        session_id = str(uuid.uuid4())
        q = queue.Queue()
        self.server.sessions[session_id] = q

        # Send endpoint event
        # Use absolute URL to avoid client-side relative resolution issues
        port = self.server.server_address[1]
        endpoint_url = "http://127.0.0.1:{}/mcp?session_id={}".format(port, session_id)
        self.log_message("Sending endpoint: %s", endpoint_url)
        self.send_sse_event("endpoint", endpoint_url)

        try:
            while not self.server.shutdown_flag:
                try:
                    msg = q.get(timeout=1.0)
                    self.send_sse_event("message", json.dumps(msg))
                except queue.Empty:
                    # Keep-alive heartbeat
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            self.log_message("Client disconnected")
            pass
        except Exception as e:
            self.log_message("SSE Error: %s", e)
        finally:
            if session_id in self.server.sessions:
                del self.server.sessions[session_id]

    def send_sse_event(self, event, data):
        try:
            self.wfile.write("event: {}\ndata: {}\n\n".format(event, data).encode("utf-8"))
            self.wfile.flush()
        except Exception as e:
            self.log_message("Send Event Error: %s", e)
            raise

    def do_POST(self):
        self.log_message("POST request to %s", self.path)
        auth_header = self.headers.get("Authorization")
        expected_token = self.server.auth_token
        if not auth_header or (
            auth_header != expected_token and auth_header != "Bearer " + expected_token
        ):
            self.send_response(401)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            request = json.loads(post_data.decode("utf-8"))
            self.log_message("RPC Request: %s", request.get("method"))

            # Extract session_id from query params if present
            from urllib.parse import urlparse, parse_qs

            query = parse_qs(urlparse(self.path).query)
            session_id = query.get("session_id", [None])[0]

            # Fallback: If no session_id in URL, use the most recent session.
            if not session_id and self.server.sessions:
                # Assuming dict preserves insertion order (Python 3.7+), the last one is the newest.
                session_id = list(self.server.sessions.keys())[-1]
                self.log_message(
                    "Using fallback session_id: %s (of %d active)",
                    session_id,
                    len(self.server.sessions),
                )
            elif not session_id:
                # It's okay to not have a session for synchronous requests like initialize
                pass

            response = self.server.delegate.handle_json_rpc(request, session_id, self.server)

            if response:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))
            else:
                self.send_response(202)  # Accepted
                self.end_headers()

        except Exception as e:
            self.log_message("POST Error: %s", e)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(
                json.dumps({"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}).encode(
                    "utf-8"
                )
            )

    def log_message(self, format, *args):
        import sys

        print("[Gemini Server] " + format % args)
        sys.stdout.flush()


class MCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, handler_class, auth_token, delegate):
        super().__init__(address, handler_class)
        self.auth_token = auth_token
        self.delegate = delegate
        self.sessions = {}
        self.shutdown_flag = False


class GeminiDelegate:
    def __init__(self):
        self.pending_diffs = {}

    def handle_json_rpc(self, request, session_id, server):
        method = request.get("method")
        params = request.get("params", {})
        msg_id = request.get("id")

        if method == "tools/list":
            return self._list_tools(msg_id)

        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            print("[Gemini Server] Tool Call:", tool_name)

            if tool_name == "openDiff":
                return self.handle_open_diff(msg_id, args, session_id, server)
            elif tool_name == "closeDiff":
                return self.handle_close_diff(msg_id, args)
            elif tool_name == "navigateTo":
                return self.handle_navigate_to(msg_id, args)
            else:
                return self.error_response(msg_id, -32601, "Method not found")

        elif method == "initialize":
            return self._handle_initialize(msg_id)

        return self.error_response(msg_id, -32601, "Method not found")

    def _list_tools(self, msg_id):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "openDiff",
                        "description": "Open a diff view for a file",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "filePath": {"type": "string"},
                                "newContent": {"type": "string"},
                                "explanation": {"type": "string"},
                                "blocking": {"type": "boolean"},
                            },
                            "required": ["filePath", "newContent"],
                        },
                    },
                    {
                        "name": "closeDiff",
                        "description": "Close a diff view",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"filePath": {"type": "string"}},
                            "required": ["filePath"],
                        },
                    },
                    {
                        "name": "navigateTo",
                        "description": "Open a file and scroll to a specific line/character",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "filePath": {"type": "string"},
                                "line": {"type": "integer"},
                                "character": {"type": "integer"},
                            },
                            "required": ["filePath", "line"],
                        },
                    },
                ]
            },
        }

    def _handle_initialize(self, msg_id):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "sublime-gemini", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }

    def handle_navigate_to(self, msg_id, args):
        path = args.get("filePath")
        line = args.get("line", 1)
        col = args.get("character", 1)
        sublime.set_timeout(lambda: self._navigate_ui(path, line, col), 0)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "Navigated to " + str(path)}]},
        }

    def _navigate_ui(self, path, line, col):
        window = sublime.active_window()
        window.open_file("{}:{}:{}".format(path, line, col), sublime.ENCODED_POSITION)

    def handle_open_diff(self, msg_id, args, session_id, server):
        file_path = args.get("filePath")
        new_content = args.get("newContent")
        explanation = args.get("explanation", "Gemini has proposed changes to this file.")
        blocking = args.get("blocking", False)

        request_context = {
            "msg_id": msg_id,
            "session_id": session_id,
            "server": server,
            "original_content": None,
            "blocking": blocking,
        }

        if blocking:
            request_context["queue"] = queue.Queue(maxsize=1)

        self.pending_diffs[file_path] = request_context
        sublime.set_timeout(lambda: self._open_diff_ui(file_path, new_content, explanation), 0)

        if blocking:
            try:
                result = request_context["queue"].get(timeout=600)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
                }
            except queue.Empty:
                return self.error_response(msg_id, -32000, "Timeout waiting for user action")

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "Diff view opened"}]},
        }

    def _prepare_diff_view(self, file_path):
        window = sublime.active_window()
        view = window.find_open_file(file_path)
        if not view:
            view = window.open_file(file_path)
        return view

    def _apply_diff_highlights(self, view, original_content, new_content):
        try:
            original_lines = original_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(None, original_lines, new_lines)

            changed_regions = []
            first_change_pt = None
            last_change_pt = None

            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag != "equal":
                    start_pt = view.text_point(j1, 0)
                    end_pt = view.text_point(j2, 0)
                    if first_change_pt is None:
                        first_change_pt = start_pt
                    last_change_pt = end_pt
                    changed_regions.append(sublime.Region(start_pt, end_pt))

            if changed_regions:
                view.add_regions(
                    "gemini_changes", changed_regions, "region.yellowish", "", sublime.DRAW_NO_FILL
                )

            if first_change_pt is not None:
                view.show_at_center(first_change_pt)
            return last_change_pt
        except Exception as e:
            print("[Gemini] Error calculating diff highlights:", e)
            return None

    def _add_diff_phantoms(self, view, file_path, explanation, last_change_pt):
        import html as html_module

        safe_explanation = html_module.escape(explanation)
        html = """
        <body id="gemini-diff">
            <style>
                body {{ background-color: #1e1e1e; padding: 12px; border-bottom: 1px solid #333; }}
                .container {{ display: flex; flex-direction: column; gap: 8px; }}
                .header {{ display: block; margin-bottom: 8px; }}
                .label {{ font-weight: bold; color: #a9b7c6; font-size: 1.1em; }}
                .explanation {{ color: #cccccc; margin-bottom: 12px; line-height: 1.4; display: block; }}
                .actions {{ margin-top: 5px; }}
                .btn {{ text-decoration: none; display: inline-block; padding: 6px 14px; border-radius: 4px; color: white; margin-right: 10px; font-size: 0.9em; font-family: system-ui; }}
                .btn.accept {{ background-color: #2da44e; }}
                .btn.reject {{ background-color: #cf222e; }}
            </style>
            <div class="container">
                <div class="header">
                    <span class="label">Gemini Suggestion</span>
                </div>
                <div class="explanation">{explanation}</div>
                <div class="actions">
                    <a href="accept" class="btn accept">Accept Changes</a>
                    <a href="reject" class="btn reject">Reject</a>
                </div>
            </div>
        </body>
        """.format(
            explanation=safe_explanation
        )

        view.erase_phantoms("gemini_diff_header")
        view.erase_phantoms("gemini_diff_footer")

        view.add_phantom(
            "gemini_diff_header",
            sublime.Region(0, 0),
            html,
            sublime.LAYOUT_BLOCK,
            lambda href: self.resolve_diff(file_path, href == "accept"),
        )

        footer_pt = last_change_pt if last_change_pt is not None else view.size()
        if footer_pt > 100:
            view.add_phantom(
                "gemini_diff_footer",
                sublime.Region(footer_pt, footer_pt),
                html,
                sublime.LAYOUT_BLOCK,
                lambda href: self.resolve_diff(file_path, href == "accept"),
            )

    def _open_diff_ui(self, file_path, new_content, explanation):
        view = self._prepare_diff_view(file_path)
        if view.is_loading():
            sublime.set_timeout(lambda: self._open_diff_ui(file_path, new_content, explanation), 50)
            return

        sublime.active_window().focus_view(view)
        original_content = view.substr(sublime.Region(0, view.size()))

        if file_path in self.pending_diffs:
            self.pending_diffs[file_path]["original_content"] = original_content

        view.set_reference_document(original_content)
        view.run_command("gemini_replace_content", {"text": new_content})
        view.settings().set("gemini_diff_file", file_path)
        view.settings().set("gemini_is_diff", True)

        last_change_pt = self._apply_diff_highlights(view, original_content, new_content)
        self._add_diff_phantoms(view, file_path, explanation, last_change_pt)

    def _accept_diff(self, view, file_path):
        if not view:
            return {
                "msg": None,
                "result": {"status": "rejected", "error": "View not found during acceptance"},
            }

        final_content = view.substr(sublime.Region(0, view.size()))
        view.erase_regions("gemini_changes")
        view.erase_phantoms("gemini_diff_header")
        view.erase_phantoms("gemini_diff_footer")
        view.settings().erase("gemini_diff_file")
        view.settings().erase("gemini_is_diff")
        view.set_reference_document("")
        sublime.status_message("Gemini: Changes accepted.")

        return {
            "msg": {
                "jsonrpc": "2.0",
                "method": "ide/diffAccepted",
                "params": {"filePath": file_path, "content": final_content},
            },
            "result": {"status": "accepted", "content": final_content},
        }

    def _reject_diff(self, view, file_path, original_content):
        if view and view.is_valid() and original_content is not None:
            view.run_command("gemini_replace_content", {"text": original_content})
            view.erase_regions("gemini_changes")
            view.erase_phantoms("gemini_diff_header")
            view.erase_phantoms("gemini_diff_footer")
            view.settings().erase("gemini_diff_file")
            view.settings().erase("gemini_is_diff")
            view.set_reference_document("")
            sublime.status_message("Gemini: Changes rejected.")

        return {
            "msg": {
                "jsonrpc": "2.0",
                "method": "ide/diffRejected",
                "params": {"filePath": file_path},
            },
            "result": {"status": "rejected"},
        }

    def resolve_diff(self, file_path, accepted):
        view = None
        for v in sublime.active_window().views():
            if v.file_name() == file_path or v.settings().get("gemini_diff_file") == file_path:
                view = v
                break

        req = self.pending_diffs.get(file_path)
        if req:
            session_id, server, original_content = (
                req["session_id"],
                req["server"],
                req["original_content"],
            )
            blocking = req.get("blocking", False)

            outcome = (
                self._accept_diff(view, file_path)
                if accepted
                else self._reject_diff(view, file_path, original_content)
            )

            if session_id in server.sessions:
                server.sessions[session_id].put(outcome["msg"])
            if blocking and "queue" in req:
                req["queue"].put(outcome["result"])
            del self.pending_diffs[file_path]

    def handle_close_diff(self, msg_id, args):
        file_path = args.get("filePath")
        sublime.set_timeout(lambda: self.resolve_diff(file_path, False), 0)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": "Diff closed"}]},
        }

    def error_response(self, msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
