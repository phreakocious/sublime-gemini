import difflib
import html
import http.server
import json
import os
import queue
import socketserver
import sys
import time
import uuid

import sublime


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

        # Handle 'Bearer <token>' or just '<token>'
        provided_token = auth_header
        if auth_header and auth_header.startswith("Bearer "):
            provided_token = auth_header[7:]

        if not auth_header or provided_token != expected_token:
            self.log_message(
                "Auth failed. Provided: %s, Expected: %s", provided_token, expected_token
            )
            self.send_response(401)
            self.end_headers()
            return

        # Read POST body if present (for initial Streamable HTTP request)
        initial_request = None
        if self.command == "POST":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    post_data = self.rfile.read(content_length)
                    initial_request = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self.log_message("Error reading initial POST data: %s", e)
                # Continue with SSE setup, but maybe log/error?
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        session_id = str(uuid.uuid4())
        q = queue.Queue(maxsize=100)
        self.server.sessions[session_id] = q

        # Send endpoint event FIRST
        # Use absolute URL to prevent client resolution issues
        host, port = self.server.server_address
        # Handle case where host is 0.0.0.0 (though we bind to 127.0.0.1 usually)
        if host == "0.0.0.0":
            host = "127.0.0.1"

        endpoint_url = "http://{}:{}/mcp?session_id={}".format(host, port, session_id)
        self.log_message("Sending endpoint: %s", endpoint_url)
        self.send_sse_event("endpoint", endpoint_url)

        # Notify that a new session has been added (now that endpoint is sent)
        if hasattr(self.server, "on_session_added") and self.server.on_session_added:
            self.server.on_session_added(session_id)

        # Process initial request if it existed
        if initial_request:
            try:
                self.log_message(
                    "Processing initial RPC from POST body: %s", initial_request.get("method")
                )
                # Note: handle_json_rpc might need session_id for context, which matches.
                response = self.server.delegate.handle_json_rpc(
                    initial_request, session_id, self.server
                )
                if response:
                    # Enqueue the response as an SSE message
                    q.put(response)
            except Exception as e:
                self.log_message("Error handling initial RPC: %s", e)
                # Maybe send an error response via SSE?
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": str(e)},
                    "id": initial_request.get("id"),
                }
                q.put(error_response)

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
            self.log_message("Client disconnected (ConnectionReset/BrokenPipe)")
            pass
        except Exception as e:
            self.log_message("SSE Error: %s", e)
        finally:
            self.log_message("SSE Handler Exiting for session %s", session_id)
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

        # Handle 'Bearer <token>' or just '<token>'
        provided_token = auth_header
        if auth_header and auth_header.startswith("Bearer "):
            provided_token = auth_header[7:]

        if not auth_header or provided_token != expected_token:
            self.log_message(
                "Auth failed. Provided: %s, Expected: %s", provided_token, expected_token
            )
            self.send_response(401)
            self.end_headers()
            return

        # Check for SSE request via POST (Streamable HTTP)
        # Only treat as new SSE session if session_id is NOT in URL.
        # Existing sessions send POSTs to /mcp?session_id=..., which should be handled as RPCs.
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        session_id_param = query.get("session_id", [None])[0]

        accept_header = self.headers.get("Accept", "")
        if "text/event-stream" in accept_header and not session_id_param:
            self.handle_sse()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            request = json.loads(post_data.decode("utf-8"))
            self.log_message("RPC Request: %s", request.get("method"))

            # Extract session_id from query params if present
            from urllib.parse import parse_qs, urlparse

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
                self.log_message("Warning: No session_id found and no active sessions available.")
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
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        print("[%s] [Gemini Server] %s" % (timestamp, format % args))
        sys.stdout.flush()


class MCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, handler_class, auth_token, delegate):
        super().__init__(address, handler_class)
        self.auth_token = auth_token
        self.delegate = delegate
        self.sessions = {}
        self.shutdown_flag = False
        self.on_session_added = None


class GeminiDelegate:
    def __init__(self):
        self.pending_diffs = {}
        self.on_tools_list = None

    def handle_json_rpc(self, request, session_id, server):
        method = request.get("method")
        params = request.get("params", {})
        msg_id = request.get("id")

        if method == "tools/list":
            print("[Gemini Server] Handling tools/list")
            return self._list_tools(msg_id)

        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            print("[Gemini Server] Tool Call:", tool_name)

            # Support both prefixed and non-prefixed calls
            base_name = tool_name[8:] if tool_name.startswith("sublime:") else tool_name

            if base_name == "openDiff":
                return self.handle_open_diff(msg_id, args, session_id, server)
            elif base_name == "closeDiff":
                return self.handle_close_diff(msg_id, args)
            elif base_name == "navigateTo":
                return self.handle_navigate_to(msg_id, args)
            else:
                return self.error_response(msg_id, -32601, "Method not found: " + tool_name)

        elif method == "initialize":
            return self._handle_initialize(msg_id)

        return self.error_response(msg_id, -32601, "Method not found")

    def _list_tools(self, msg_id):
        if self.on_tools_list:
            self.on_tools_list()
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "sublime:openDiff",
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

    def _get_target_window(self, file_path=None):
        # 1. If file_path is provided, try to find a window that has this file open
        if file_path:
            for w in sublime.windows():
                if w.find_open_file(file_path):
                    return w

        # 2. Try to find a window that contains the file in its project folders
        if file_path:
            abs_path = os.path.abspath(file_path)
            best_match = None
            max_len = 0

            for w in sublime.windows():
                for folder in w.folders():
                    if abs_path.startswith(os.path.abspath(folder)):
                        if len(folder) > max_len:
                            max_len = len(folder)
                            best_match = w

            if best_match:
                return best_match

        # 3. Fallback to active window
        return sublime.active_window()

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
            view.erase_phantoms("gemini_diff_deleted")

            first_change_pt = None
            last_change_pt = None

            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    continue

                start_pt = view.text_point(j1, 0)
                end_pt = view.text_point(j2, 0)

                if first_change_pt is None:
                    first_change_pt = start_pt
                last_change_pt = end_pt

                if tag in ("insert", "replace"):
                    changed_regions.append(sublime.Region(start_pt, end_pt))
                elif tag == "delete":
                    # Add a zero-width region for navigation to deleted blocks
                    changed_regions.append(sublime.Region(start_pt, start_pt))

                if tag in ("delete", "replace"):
                    deleted_text = "".join(original_lines[i1:i2])
                    safe_text = html.escape(deleted_text)

                    phantom_html = """
                    <body id="gemini-diff-deleted">
                        <style>
                            body {
                                background-color: #4b1818;
                                color: #cccccc;
                                margin: 0;
                                padding: 1px 4px;
                                border: 1px solid #6b2828;
                            }
                        </style>
                        <div style="font-family: monospace; white-space: pre;">%s</div>
                    </body>
                    """ % (
                        safe_text
                    )

                    view.add_phantom(
                        "gemini_diff_deleted",
                        sublime.Region(start_pt, start_pt),
                        phantom_html,
                        sublime.LAYOUT_BLOCK,
                    )

            if changed_regions:
                # 'markup.inserted' is a standard scope for green/inserted text
                view.add_regions(
                    "gemini_changes", changed_regions, "markup.inserted", "", sublime.DRAW_NO_FILL
                )

            if first_change_pt is not None:
                view.show_at_center(first_change_pt)
            return last_change_pt
        except Exception as e:
            print("[Gemini] Error calculating diff highlights:", e)
            return None

    def _get_diff_toolbar_html(self, terminal_panel=None, warning=False):
        terminal_btn = ""
        if terminal_panel:
            terminal_btn = '<a href="terminal:{}" style="text-decoration: none; padding: 4px 10px; border-radius: 4px; color: #aaaaaa; background-color: #2d2d2d; font-size: 0.8rem;">Terminal</a>'.format(
                terminal_panel
            )

        warning_html = ""
        if warning:
            warning_html = '<span style="color: #e5bf38; font-size: 0.9rem; margin-right: 12px; font-weight: bold;" title="File has unsaved changes. Diff might be based on stale content.">\u26A0 Unsaved Changes</span>'

        return """
        <body id="gemini-diff-toolbar" style="background-color: #1e1e1e; margin: 0; padding: 12px; border-top: 1px solid #333;">
            <div style="display: flex; flex-direction: row; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div style="display: flex; flex-direction: row; align-items: center;">
                    <span style="font-weight: bold; color: #cccccc; font-size: 0.8rem; margin-right: 16px; text-transform: uppercase; letter-spacing: 0.5px;">GEMINI REVIEW</span>
                    {}
                    <a href="info" style="text-decoration: none; padding: 4px 10px; border-radius: 4px; color: #aaaaaa; background-color: #2d2d2d; font-size: 0.8rem; margin-right: 8px;">Info</a>
                    {}
                </div>
            </div>
            <div style="display: flex; flex-direction: row; justify-content: flex-end; align-items: center;">
                <a href="prev" style="text-decoration: none; padding: 6px 14px; border-radius: 4px; color: #ffffff; background-color: #007acc; font-size: 0.8rem; margin-left: 8px;">\u2190 Prev</a>
                <a href="next" style="text-decoration: none; padding: 6px 14px; border-radius: 4px; color: #ffffff; background-color: #007acc; font-size: 0.8rem; margin-left: 8px;">Next \u2192</a>
                <a href="accept" style="text-decoration: none; padding: 6px 14px; border-radius: 4px; color: #ffffff; background-color: #2ea043; font-weight: bold; font-size: 0.8rem; margin-left: 8px;">\u2713 Accept</a>
                <a href="reject" style="text-decoration: none; padding: 6px 14px; border-radius: 4px; color: #ffffff; background-color: #da3633; font-size: 0.8rem; margin-left: 8px;">\u2715 Reject</a>
            </div>
        </body>
        """.format(
            warning_html, terminal_btn
        )

    def _show_diff_panel(self, window, file_path, previous_panel=None, warning=False):
        panel = window.create_output_panel("GeminiDiff")
        panel.settings().set("gutter", False)
        panel.settings().set("line_numbers", False)

        html = self._get_diff_toolbar_html(previous_panel, warning)

        panel.erase_phantoms("gemini_diff_panel")
        panel.add_phantom(
            "gemini_diff_panel",
            sublime.Region(0, 0),
            html,
            sublime.LAYOUT_BLOCK,
            lambda href: self.handle_diff_action(file_path, href),
        )
        window.run_command("show_panel", {"panel": "output.GeminiDiff"})

    def _add_diff_phantoms(self, view, file_path):
        # We only use the output panel for controls now, so just clean up any old phantoms.
        view.erase_phantoms("gemini_diff_header")
        view.erase_phantoms("gemini_diff_footer")

    def handle_diff_action(self, file_path, action):
        if action.startswith("terminal:"):
            panel_name = action.split(":", 1)[1]
            sublime.active_window().run_command("show_panel", {"panel": panel_name})
            return

        view = None
        for v in sublime.active_window().views():
            if v.file_name() == file_path or v.settings().get("gemini_diff_file") == file_path:
                view = v
                break

        if not view:
            return

        if action == "info":
            explanation = view.settings().get("gemini_diff_explanation", "No explanation provided.")

            safe_explanation = html.escape(explanation).replace("\n", "<br>")

            popup_html = """
            <body id="gemini-info">
                <style>
                    body {{ padding: 10px; font-size: 0.9em; line-height: 1.5; }}
                    h3 {{ margin-top: 0; color: #a9b7c6; }}
                </style>
                <h3>Gemini Suggestion</h3>
                <div>{}</div>
            </body>
            """.format(
                safe_explanation
            )

            view.show_popup(popup_html, max_width=600)

        elif action == "next":
            view.run_command("gemini_next_change")
        elif action == "prev":
            view.run_command("gemini_prev_change")
        elif action == "accept":
            self.resolve_diff(file_path, True)
        elif action == "reject":
            self.resolve_diff(file_path, False)

    def _open_diff_ui(self, file_path, new_content, explanation):
        # Capture panel before any focus changes
        window = self._get_target_window(file_path)
        current_panel = window.active_panel()

        view = self._prepare_diff_view(file_path)
        if view.is_loading():
            sublime.set_timeout(lambda: self._open_diff_ui(file_path, new_content, explanation), 50)
            return

        window.focus_view(view)
        original_content = view.substr(sublime.Region(0, view.size()))
        is_dirty = view.is_dirty()

        if file_path in self.pending_diffs:
            self.pending_diffs[file_path]["original_content"] = original_content
            self.pending_diffs[file_path]["previous_panel"] = current_panel

        view.set_reference_document(original_content)
        view.run_command("gemini_replace_content", {"text": new_content})
        view.settings().set("gemini_diff_file", file_path)
        view.settings().set("gemini_is_diff", True)
        view.settings().set("gemini_diff_explanation", explanation)

        # Move heavy diff calculation to background thread
        sublime.set_timeout_async(
            lambda: self._async_calc_diff(
                view, file_path, original_content, new_content, window, current_panel, is_dirty
            ),
            0,
        )

    def _async_calc_diff(
        self, view, file_path, original_content, new_content, window, current_panel, is_dirty
    ):
        # Calculate highlights (expensive)
        # Note: _apply_diff_highlights returns None but applies changes to view.
        # Since view methods must run on main thread, we must split the calculation from application
        # or rely on Sublime's thread safety for add_phantom/add_regions if they are safe (they often aren't).
        # Standard practice: Compute diff ops in background, apply to view in main thread.

        try:
            original_lines = original_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(None, original_lines, new_lines)
            opcodes = list(matcher.get_opcodes())  # Realize list in background

            # Schedule application on main thread
            sublime.set_timeout(
                lambda: self._apply_diff_ui(
                    view, file_path, opcodes, original_lines, window, current_panel, is_dirty
                ),
                0,
            )
        except Exception as e:
            print("[Gemini] Async diff error:", e)

    def _apply_diff_ui(
        self, view, file_path, opcodes, original_lines, window, current_panel, is_dirty
    ):
        if not view.is_valid():
            return

        changed_regions = []
        view.erase_phantoms("gemini_diff_deleted")

        first_change_pt = None

        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                continue

            start_pt = view.text_point(j1, 0)
            end_pt = view.text_point(j2, 0)

            if first_change_pt is None:
                first_change_pt = start_pt

            if tag in ("insert", "replace"):
                changed_regions.append(sublime.Region(start_pt, end_pt))
            elif tag == "delete":
                changed_regions.append(sublime.Region(start_pt, start_pt))

            if tag in ("delete", "replace"):
                deleted_text = "".join(original_lines[i1:i2])
                safe_text = html.escape(deleted_text)

                phantom_html = """
                <body id="gemini-diff-deleted">
                    <style>
                        body {
                            background-color: #4b1818;
                            color: #cccccc;
                            margin: 0;
                            padding: 1px 4px;
                            border: 1px solid #6b2828;
                        }
                    </style>
                    <div style="font-family: monospace; white-space: pre;">%s</div>
                </body>
                """ % (
                    safe_text
                )

                view.add_phantom(
                    "gemini_diff_deleted",
                    sublime.Region(start_pt, start_pt),
                    phantom_html,
                    sublime.LAYOUT_BLOCK,
                )

        if changed_regions:
            view.add_regions(
                "gemini_changes", changed_regions, "markup.inserted", "", sublime.DRAW_NO_FILL
            )

        if first_change_pt is not None:
            view.show_at_center(first_change_pt)

        self._add_diff_phantoms(view, file_path)
        self._show_diff_panel(window, file_path, current_panel, is_dirty)

    def _apply_diff_highlights(self, view, original_content, new_content):
        # Legacy method kept/modified if needed or removed?
        # I'll keep a stub or remove it. The logic is moved to _async_calc_diff/_apply_diff_ui.
        pass

    def _accept_diff(self, view, file_path, previous_panel=None):
        if not view:
            return {
                "msg": None,
                "result": {"status": "rejected", "error": "View not found during acceptance"},
            }

        if view.window():
            view.window().run_command("hide_panel", {"panel": "output.GeminiDiff"})
            if previous_panel:
                view.window().run_command("show_panel", {"panel": previous_panel})

        final_content = view.substr(sublime.Region(0, view.size()))
        view.erase_regions("gemini_changes")
        view.erase_phantoms("gemini_diff_header")
        view.erase_phantoms("gemini_diff_footer")
        view.erase_phantoms("gemini_diff_deleted")
        view.settings().erase("gemini_diff_file")
        view.settings().erase("gemini_is_diff")
        view.settings().erase("gemini_diff_explanation")
        view.set_reference_document(final_content)

        view.run_command("save")
        sublime.status_message("Gemini: Changes accepted and saved.")

        return {
            "msg": {
                "jsonrpc": "2.0",
                "method": "ide/diffAccepted",
                "params": {"filePath": file_path, "content": final_content},
            },
            "result": {"status": "accepted", "content": final_content},
        }

    def _reject_diff(self, view, file_path, original_content, previous_panel=None):
        if view and view.is_valid() and original_content is not None:
            if view.window():
                view.window().run_command("hide_panel", {"panel": "output.GeminiDiff"})
                if previous_panel:
                    view.window().run_command("show_panel", {"panel": previous_panel})

            view.run_command("gemini_replace_content", {"text": original_content})
            view.erase_regions("gemini_changes")
            view.erase_phantoms("gemini_diff_header")
            view.erase_phantoms("gemini_diff_footer")
            view.erase_phantoms("gemini_diff_deleted")
            view.settings().erase("gemini_diff_file")
            view.settings().erase("gemini_is_diff")
            view.settings().erase("gemini_diff_explanation")
            view.set_reference_document(original_content)
            # Removed force save to respect original dirty state
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
            previous_panel = req.get("previous_panel")

            outcome = (
                self._accept_diff(view, file_path, previous_panel)
                if accepted
                else self._reject_diff(view, file_path, original_content, previous_panel)
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
