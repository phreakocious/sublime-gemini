import json
import os
import subprocess
import tempfile
import threading
import time
import uuid

import sublime
import sublime_plugin

try:
    from . import gemini_server
except Exception:
    import gemini_server

# --- Global State ---
server, discovery_file_path, settings_file_path = None, None, None


# --- Helpers ---
def get_gemini_path():
    """Retrieves the gemini executable path from settings."""
    return sublime.load_settings("Gemini.sublime-settings").get("gemini_path", "gemini")


def get_project_roots(window):
    roots = []
    if not window:
        return roots
    if window.project_data() and "folders" in window.project_data():
        for f in window.project_data()["folders"]:
            p = f["path"]
            if not os.path.isabs(p) and window.extract_variables().get("project_path"):
                p = os.path.join(window.extract_variables()["project_path"], p)
            roots.append(os.path.abspath(p))
    v = window.active_view()
    if v and v.file_name():
        path = os.path.abspath(os.path.dirname(v.file_name()))
        curr = path
        while curr and os.path.dirname(curr) != curr:
            if os.path.exists(os.path.join(curr, ".git")):
                abs_curr = os.path.abspath(curr)
                # Only add the implicit root if we have no explicit project roots
                if not roots and abs_curr not in roots:
                    roots.append(abs_curr)
                break
            curr = os.path.dirname(curr)
        if not roots:
            roots.append(path)
    return roots


def get_symbol_at_point(view, point):
    best_s, best_r = None, None
    for r, s in view.symbols():
        if r.contains(point):
            if best_r is None or r.size() < best_r.size():
                best_r, best_s = r, s
    return best_s


def get_target_region(view):
    s = view.sel()
    if not s:
        return None
    target = s[0]
    symbol = get_symbol_at_point(view, target.begin())
    if symbol and target.size() < 50:
        for r, name in view.symbols():
            if name == symbol and r.contains(target.begin()):
                target = r
                break
    if target.empty():
        target = view.line(target)
    return target if not target.empty() else None


def format_context_string(view, target, roots):
    fname = view.file_name() or "Untitled"
    for r in roots:
        if fname.startswith(r):
            try:
                fname = os.path.relpath(fname, r)
                break
            except ValueError:
                pass

    rc_start = view.rowcol(target.begin())
    rc_end = view.rowcol(target.end())
    l_start, l_end = rc_start[0] + 1, rc_end[0] + 1
    l_info = "Line {}".format(l_start) if l_start == l_end else "Lines {}-{}".format(l_start, l_end)

    symbol = get_symbol_at_point(view, target.begin())
    sym_info = ' inside "{}"'.format(symbol) if symbol else ""

    return "(Context: file='{}', {} {})".format(fname, l_info, sym_info)


def get_selection_metadata(view, roots):
    if not view:
        return ""
    target = get_target_region(view)
    if not target:
        return ""
    return format_context_string(view, target, roots)


def push_notification(method, params):
    global server
    if not server:
        return
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    for session_id in list(server.sessions.keys()):
        try:
            server.sessions[session_id].put(msg)
        except Exception:
            pass


def push_context_update(window):
    if not window:
        return

    roots = get_project_roots(window)
    open_files = []

    active_view = window.active_view()
    # If the active view is a Terminus view, try to find the last active code view
    # (This is an approximation; ideally we'd track Z-order)
    if active_view and active_view.settings().get("terminus_view.tag"):
        active_view = None
        # Fallback: find the first non-terminus view
        for v in window.views():
            if not v.settings().get("terminus_view.tag"):
                active_view = v
                break

    for view in window.views():
        fname = view.file_name()
        if not fname or not os.path.exists(fname):
            continue

        # Ignore Terminus views in the file list (though they usually don't have file_names)
        if view.settings().get("terminus_view.tag"):
            continue

        # Check if this view is the currently active one
        is_active = active_view and view.id() == active_view.id()

        # Check if file is within any of the project roots
        is_in_project = False
        for r in roots:
            # Check for exact match or subdirectory
            if fname == r or fname.startswith(os.path.join(r, "")):
                is_in_project = True
                break

        if not is_in_project and not is_active:
            continue

        sel = view.sel()
        selected_text = ""
        cursor = {"line": 1, "character": 1}
        if len(sel) > 0:
            region = sel[0]
            if not region.empty():
                selected_text = view.substr(region)[:16384]  # Limit to 16KB
            row, col = view.rowcol(region.begin())
            cursor = {"line": row + 1, "character": col + 1}

        open_files.append(
            {
                "path": fname,
                "timestamp": int(time.time()),  # Ideally we'd track last access time
                "isActive": is_active,
                "selectedText": selected_text,
                "cursor": cursor,
            }
        )

    params = {"workspaceState": {"openFiles": open_files, "isTrusted": True}}
    push_notification("ide/contextUpdate", params)


def write_settings_file():
    global settings_file_path
    if settings_file_path and os.path.exists(settings_file_path):
        return settings_file_path

    settings_dir = os.path.join(tempfile.gettempdir(), "gemini", "settings")
    if not os.path.exists(settings_dir):
        try:
            os.makedirs(settings_dir)
        except Exception:
            pass

    settings_file_path = os.path.join(
        settings_dir, "gemini-ide-settings-{}.json".format(os.getpid())
    )
    try:
        config = {"ide": {"enabled": True}}
        if server:
            config["mcpServers"] = {
                "sublime": {
                    "url": "http://127.0.0.1:{}/mcp".format(server.server_address[1]),
                    "headers": {"Authorization": server.auth_token},
                    "trust": True,
                }
            }
        with open(settings_file_path, "w") as f:
            json.dump(config, f)
        return settings_file_path
    except Exception:
        return None


def write_launcher_script():
    launcher_code = r"""
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn } = require('child_process');

function main() {
    const tmp = os.tmpdir();
    const ideDir = path.join(tmp, 'gemini', 'ide');
    let port = '';
    let token = '';

    try {
        if (fs.existsSync(ideDir)) {
            const files = fs.readdirSync(ideDir)
                .filter(f => f.startsWith('gemini-ide-server-') && f.endsWith('.json'))
                .map(f => ({ name: f, time: fs.statSync(path.join(ideDir, f)).mtimeMs }))
                .sort((a, b) => b.time - a.time);

            if (files.length > 0) {
                const data = JSON.parse(fs.readFileSync(path.join(ideDir, files[0].name), 'utf8'));
                port = String(data.port || '');
                token = data.authToken || '';
            }
        }
    } catch (e) {}

    const env = { ...process.env };
    env.GEMINI_CLI_IDE_SERVER_PORT = port;
    env.GEMINI_CLI_IDE_AUTH_TOKEN = token;
    env.TERM_PROGRAM = 'vscode';

    if (port) {
        const settings = {
            ide: { enabled: true },
            tools: { allowed: ['openDiff', 'closeDiff', 'navigateTo'] },
            mcpServers: {
                sublime: {
                    url: `http://127.0.0.1:${port}/mcp`,
                    headers: { Authorization: token },
                    trust: true
                }
            }
        };
        try {
            const settingsPath = path.join(tmp, `gemini-ide-settings-${process.pid}.json`);
            fs.writeFileSync(settingsPath, JSON.stringify(settings));
            env.GEMINI_CLI_SYSTEM_SETTINGS_PATH = settingsPath;
        } catch (e) {}
    }

    const args = process.argv.slice(2);
    if (args.length === 0) process.exit(1);

    const child = spawn(args[0], args.slice(1), {
        stdio: 'inherit',
        env: env,
        shell: os.platform() === 'win32'
    });

    child.on('exit', (code) => process.exit(code || 0));
    child.on('error', (err) => {
        console.error('Failed to start Gemini:', err.message);
        process.exit(1);
    });
}

main();
"""
    try:
        launcher_dir = os.path.join(tempfile.gettempdir(), "gemini", "scripts")
        if not os.path.exists(launcher_dir):
            os.makedirs(launcher_dir)

        launcher_path = os.path.join(launcher_dir, "gemini_launcher.cjs")
        with open(launcher_path, "w") as f:
            f.write(launcher_code)
        return launcher_path
    except Exception:
        return None


# --- Commands ---
class GeminiChatCommand(sublime_plugin.WindowCommand):
    def run(self, instruction=None, location=None):
        """
        Runs the Gemini CLI in a Terminus view. If an instruction is provided,
        it sends it to the terminal.
        """
        # Capture context before we possibly switch focus to the terminal
        source_view = self.window.active_view()
        context_text = ""
        if source_view and instruction:
            # Ensure we don't grab text from the terminal itself
            if not source_view.settings().get("terminus_view.tag"):
                target = get_target_region(source_view)
                if target:
                    context_text = source_view.substr(target)

        # Append context to instruction if available
        if instruction and context_text:
            instruction += "\n\n```\n" + context_text + "\n```"

        # Snapshot active sessions before launch to detect new connection
        initial_sessions = set(server.sessions.keys()) if server else set()

        terminus_view, is_new = self.ensure_terminus_open(location)
        if not terminus_view:
            return

        self.window.focus_view(terminus_view)
        if instruction:
            if is_new:
                # Wait for the new CLI instance to connect to the MCP server
                self.wait_for_new_session_and_send(instruction, initial_sessions)
            else:
                self.send_instruction(instruction)

    def wait_for_new_session_and_send(self, instruction, initial_sessions, attempt=0):
        if not server:
            self.send_instruction(instruction)
            return

        current_sessions = set(server.sessions.keys())
        # Check if a new session has appeared
        if len(current_sessions) > len(initial_sessions) and (current_sessions - initial_sessions):
            # New session detected! Give it a moment to settle (REPL initialization)
            sublime.set_timeout(lambda: self.send_instruction(instruction), 1000)
        elif attempt < 40:  # Poll for ~20 seconds (40 * 500ms)
            sublime.set_timeout(
                lambda: self.wait_for_new_session_and_send(
                    instruction, initial_sessions, attempt + 1
                ),
                500,
            )
        else:
            # Timeout waiting for connection, try sending anyway
            print("[Gemini] Timeout waiting for MCP connection, sending instruction blindly.")
            self.send_instruction(instruction)

    def send_instruction(self, instruction):
        self.window.run_command(
            "terminus_send_string", {"string": instruction + "\n", "tag": "gemini_cli"}
        )

    def description(self, instruction=None, location=None):
        return "Gemini Chat: {}".format(instruction) if instruction else "Gemini Chat"

    def get_terminus_env(self, roots, cmd_args):
        env = {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "FORCE_COLOR": "1",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM_PROGRAM": "vscode",
        }

        # Load user-defined environment variables
        user_env = sublime.load_settings("Gemini.sublime-settings").get("environment", {})
        if user_env:
            env.update(user_env)

        settings_path = write_settings_file()
        if settings_path:
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = settings_path

        if server:
            env["GEMINI_CLI_IDE_SERVER_PORT"] = str(server.server_address[1])
            env["GEMINI_CLI_IDE_AUTH_TOKEN"] = server.auth_token
            if roots:
                env["GEMINI_CLI_IDE_WORKSPACE_PATH"] = os.pathsep.join(roots)

        return env

    def get_shell_cmd(self, cmd_args):
        launcher_path = write_launcher_script()
        if not launcher_path:
            return cmd_args

        # We assume 'node' is in PATH since Gemini CLI requires it
        return ["node", launcher_path] + cmd_args

    def _find_existing_terminus(self, location, panel_name, tag, current_port):
        if location == "panel":
            target_view = self.window.find_output_panel(panel_name)
            if target_view:
                view_port = target_view.settings().get("gemini_server_port")
                if current_port and view_port != current_port:
                    self.window.run_command("terminus_close", {"panel": "output." + panel_name})
                    return None
            return target_view

        for v in self.window.views():
            if v.settings().get("terminus_view.tag") == tag:
                view_port = v.settings().get("gemini_server_port")
                if current_port and view_port != current_port:
                    v.close()
                    break
                return v
        return None

    def _prepare_view_location(self, location):
        if location == "split":
            if self.window.num_groups() == 1:
                self.window.set_layout(
                    {
                        "cols": [0.0, 1.0],
                        "rows": [0.0, 0.6, 1.0],
                        "cells": [[0, 0, 1, 1], [0, 1, 1, 2]],
                    }
                )
            self.window.focus_group(self.window.num_groups() - 1)

    def _create_terminus_view(self, location, panel_name, title, tag):
        roots = get_project_roots(self.window)
        cmd_args = [get_gemini_path()]
        for r in roots:
            cmd_args.extend(["--include-directories", r])
        cwd = roots[0] if roots else os.path.expanduser("~")

        env = self.get_terminus_env(roots, cmd_args)
        cmd_args = self.get_shell_cmd(cmd_args)

        print("[Gemini] Launching Terminus with env:", env)

        args = {
            "cmd": cmd_args,
            "cwd": cwd,
            "title": title,
            "auto_close": False,
            "env": env,
            "tag": tag,
        }

        if location == "panel":
            args["panel_name"] = panel_name

        self.window.run_command("terminus_open", args)

        view = None
        if location == "panel":
            view = self.window.find_output_panel(panel_name)
        else:
            view = self.window.active_view()

        if view:
            view.settings().set("gemini_context_roots", roots)

        return view

    def ensure_terminus_open(self, location=None):
        global server
        current_port = server.server_address[1] if server else None
        settings = sublime.load_settings("Gemini.sublime-settings")
        current_roots = get_project_roots(self.window)

        # If location is explicitly provided, we strict-check that location.
        # If not, we try to find ANY existing instance to reuse.
        strict_location = location is not None
        if not location:
            location = settings.get("view_location", "split")

        panel_name, title, tag = "Gemini CLI", "Gemini CLI", "gemini_cli"

        target_view = None

        # 1. Search for existing split/tab view
        for v in self.window.views():
            v_tag = v.settings().get("terminus_view.tag")
            if v_tag == tag:
                target_view = v
                break

        # 2. Search for existing panel (if not found view, or if strict location requires panel)
        if not target_view or (strict_location and location == "panel"):
            panel_view = self.window.find_output_panel(panel_name)
            if panel_view:
                # If we found a split view but strict location is panel, ignore the split view
                if strict_location and location == "panel":
                    target_view = panel_view
                # If we didn't find a split view, use the panel
                elif not target_view:
                    target_view = panel_view

        # 3. If strict location is "split" and we found a panel, ignore panel (force new split)
        if (
            strict_location
            and location == "split"
            and target_view == self.window.find_output_panel(panel_name)
        ):
            target_view = None

        # Validate port AND roots reuse
        if target_view:
            view_port = target_view.settings().get("gemini_server_port")
            view_roots = target_view.settings().get("gemini_context_roots")

            # Check 1: Port mismatch
            port_mismatch = current_port and view_port and view_port != current_port

            # Check 2: Roots mismatch (CLI scope changed)
            # If view_roots is missing (legacy view), we assume it's valid and backfill it.
            roots_mismatch = False
            if view_roots is not None:
                roots_mismatch = set(view_roots) != set(current_roots)
            else:
                target_view.settings().set("gemini_context_roots", current_roots)

            if port_mismatch or roots_mismatch:
                # Mismatch: close and recreate
                if target_view == self.window.find_output_panel(panel_name):
                    self.window.run_command("terminus_close", {"panel": "output." + panel_name})
                else:
                    target_view.close()
                target_view = None
            else:
                # Match: Reuse!
                if current_port and not view_port:
                    target_view.settings().set("gemini_server_port", current_port)
                # Ensure we update roots if they were missing
                if not view_roots:
                    target_view.settings().set("gemini_context_roots", current_roots)

                if target_view == self.window.find_output_panel(panel_name):
                    self.window.run_command("show_panel", {"panel": "output." + panel_name})
                else:
                    self.window.focus_view(target_view)
                return target_view, False

        # If we get here, no valid existing instance found. Create new.
        if not sublime.find_resources("Terminus.sublime-settings"):
            sublime.error_message("Install Terminus.")
            return None, False

        self._prepare_view_location(location)
        target_view = self._create_terminus_view(location, panel_name, title, tag)

        if target_view and current_port:
            target_view.settings().set("gemini_server_port", current_port)

        return target_view, True


class GeminiStopCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.window.run_command("terminus_keypress", {"key": "ctrl+c", "tag": "gemini_cli"})


class GeminiDebugEnvCommand(sublime_plugin.WindowCommand):
    def run(self):
        env = {"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"}
        global server
        if server:
            env["GEMINI_CLI_IDE_SERVER_PORT"] = str(server.server_address[1])

        # Load user-defined environment variables
        user_env = sublime.load_settings("Gemini.sublime-settings").get("environment", {})
        if user_env:
            env.update(user_env)

        # Ensure IDE mode is enabled by injecting a system settings file
        settings_path = write_settings_file()
        if settings_path:
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = settings_path

        self.window.run_command(
            "terminus_open",
            {
                "cmd": ["/usr/bin/env"],
                "cwd": os.path.expanduser("~"),
                "title": "Gemini Env Debug",
                "auto_close": False,
                "env": env,
            },
        )


class GeminiInsertContextCommand(sublime_plugin.WindowCommand):
    def _find_terminal_view(self, tag):
        for v in self.window.views():
            if v.settings().get("terminus_view.tag") == tag:
                return v, False

        panel_view = self.window.find_output_panel("Gemini CLI")
        if panel_view:
            return panel_view, True
        return None, False

    def _find_source_view(self, terminus_view, is_panel):
        code_view = self.window.active_view()
        if code_view and terminus_view and not is_panel and code_view.id() == terminus_view.id():
            code_view = self.window.active_view_in_group(0)
            if code_view and code_view.id() == terminus_view.id():
                code_view = self.window.active_view_in_group(1)

            if not code_view or code_view.id() == terminus_view.id():
                code_view = None
                for v in self.window.views():
                    if v.id() != terminus_view.id() and v.file_name():
                        code_view = v
                        break
        return code_view

    def run(self):
        tag = "gemini_cli"
        terminus_view, is_panel = self._find_terminal_view(tag)
        code_view = self._find_source_view(terminus_view, is_panel)

        if not code_view:
            return

        roots = get_project_roots(self.window)
        context_str = get_selection_metadata(code_view, roots)
        if not context_str:
            return

        if is_panel:
            self.window.run_command("show_panel", {"panel": "output.Gemini CLI"})
            self.window.focus_view(terminus_view)
        elif terminus_view:
            self.window.focus_view(terminus_view)

        self.window.run_command(
            "terminus_send_string", {"string": " " + context_str + " ", "tag": tag}
        )


class GeminiGenerateCommitMessageCommand(sublime_plugin.WindowCommand):
    def run(self):
        roots = get_project_roots(self.window)
        if not roots:
            sublime.status_message("Gemini: No project root found.")
            return

        # Use the first root for now
        cwd = roots[0]

        try:
            # Try staged changes first
            diff = subprocess.check_output(
                ["git", "diff", "--staged"], cwd=cwd, stderr=subprocess.STDOUT
            ).decode("utf-8")

            if not diff.strip():
                # Fallback to unstaged changes
                diff = subprocess.check_output(
                    ["git", "diff"], cwd=cwd, stderr=subprocess.STDOUT
                ).decode("utf-8")

            if not diff.strip():
                sublime.status_message("Gemini: No changes detected (staged or unstaged).")
                return

            # Limit diff size to avoid context overflow (approx 800 lines)
            lines = diff.splitlines()
            if len(lines) > 800:
                diff = "\n".join(lines[:800]) + "\n... (truncated)"

            instruction = "Generate a concise and descriptive git commit message for the following changes:\n\n```diff\n{}\n```".format(
                diff
            )
            self.window.run_command("gemini_chat", {"instruction": instruction})

        except subprocess.CalledProcessError:
            sublime.status_message("Gemini: Failed to run git diff. Is this a git repository?")
        except Exception as e:
            sublime.status_message("Gemini: Error generating commit message: {}".format(str(e)))


class GeminiInlineCommand(sublime_plugin.TextCommand):
    def run(self, edit, instruction=None):
        self.view.window().run_command("gemini_chat", {"instruction": instruction})


class GeminiReplaceContentCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=None):
        if text is None:
            return
        # Replace entire buffer content
        region = sublime.Region(0, self.view.size())
        self.view.replace(edit, region, text)


# --- Server ---
def start_server_async():
    global server
    try:
        if server:
            print("[Gemini] Server already running on port", server.server_address[1])
            return

        token = str(uuid.uuid4())
        delegate = gemini_server.GeminiDelegate()

        # Sticky port logic
        pid = os.getppid()
        ide_dir = os.path.join(tempfile.gettempdir(), "gemini", "ide")
        if not os.path.exists(ide_dir):
            try:
                os.makedirs(ide_dir)
            except Exception:
                pass

        sticky_port_file = os.path.join(ide_dir, "gemini-port-{}.txt".format(pid))
        port = 0

        if os.path.exists(sticky_port_file):
            try:
                with open(sticky_port_file, "r") as f:
                    port = int(f.read().strip())
            except Exception:
                pass

        try:
            server = gemini_server.MCPServer(
                ("127.0.0.1", port), gemini_server.MCPServerHandler, token, delegate
            )
        except OSError:
            if port != 0:
                print(
                    "[Gemini] Could not bind to sticky port {}, falling back to random".format(port)
                )
                server = gemini_server.MCPServer(
                    ("127.0.0.1", 0), gemini_server.MCPServerHandler, token, delegate
                )
            else:
                raise

        actual_port = server.server_address[1]

        # Save sticky port
        try:
            with open(sticky_port_file, "w") as f:
                f.write(str(actual_port))
        except Exception:
            pass

        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("[Gemini] Server started on port " + str(actual_port))
        sublime.set_timeout(lambda: write_settings_file(), 50)
        sublime.set_timeout(lambda: write_discovery_file(sublime.active_window()), 100)
    except Exception as e:
        print("[Gemini] Failed to start server:", e)


def write_discovery_file(window):
    global discovery_file_path
    if not window or not server:
        return
    port, token = server.server_address[1], server.auth_token
    roots = get_project_roots(window)
    ws = os.pathsep.join(roots) if roots else tempfile.gettempdir()

    # Write discovery files for both parent (plugin_host) and grandparent (Sublime Text)
    # to ensure gemini-cli can find it regardless of how it traverses the tree.
    pids = {os.getpid(), os.getppid()}

    # Try to find the grandparent PID as well (Sublime Text main process)
    try:
        import subprocess

        ppid = os.getppid()
        grandparent = (
            subprocess.check_output(["ps", "-o", "ppid=", "-p", str(ppid)]).decode().strip()
        )
        if grandparent and grandparent.isdigit():
            pids.add(int(grandparent))
    except Exception:
        pass

    discovery_dir = os.path.join(tempfile.gettempdir(), "gemini", "ide")
    if not os.path.exists(discovery_dir):
        try:
            os.makedirs(discovery_dir)
        except Exception:
            pass

    for pid in pids:
        discovery_file_path = os.path.join(
            discovery_dir, "gemini-ide-server-{}-{}.json".format(pid, port)
        )
        info = {
            "port": port,
            "workspacePath": ws,
            "authToken": token,
            "ideInfo": {"name": "sublime_text", "displayName": "Sublime Text"},
        }
        try:
            with open(discovery_file_path, "w") as f:
                json.dump(info, f)
        except Exception:
            pass


def plugin_loaded():
    # Small delay to ensure Sublime is ready
    sublime.set_timeout(lambda: threading.Thread(target=start_server_async).start(), 1000)


def plugin_unloaded():
    global server, settings_file_path
    if server:
        server.shutdown_flag = True
        server.shutdown()
        server.server_close()
        server = None
    if discovery_file_path and os.path.exists(discovery_file_path):
        os.remove(discovery_file_path)
    if settings_file_path and os.path.exists(settings_file_path):
        os.remove(settings_file_path)


class GeminiEventListener(sublime_plugin.EventListener):
    """
    Listens for Sublime Text events to update Gemini's context and handle diff views.
    """

    def on_activated(self, view):
        if view.window():
            write_discovery_file(view.window())
            sublime.set_timeout_async(lambda: push_context_update(view.window()), 50)

    def on_selection_modified(self, view):
        if view.window():
            sublime.set_timeout_async(lambda: push_context_update(view.window()), 100)

    def on_close(self, view):
        # Check if the closed view was a diff view
        diff_file = view.settings().get("gemini_diff_file")
        if diff_file and server and hasattr(server, "delegate"):
            # Resolve as rejected (false) if it wasn't explicitly accepted.
            # The resolve_diff method handles the logic to ensure we don't double-resolve.
            server.delegate.resolve_diff(diff_file, False)


class GeminiAcceptDiffCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        diff_file = self.view.settings().get("gemini_diff_file")
        if not diff_file:
            return

        global server
        if server and hasattr(server, "delegate"):
            server.delegate.resolve_diff(diff_file, True)
        else:
            content = self.view.substr(sublime.Region(0, self.view.size()))
            try:
                with open(diff_file, "w") as f:
                    f.write(content)
                self.view.close()
            except Exception:
                pass

    def is_visible(self):
        return self.view.settings().get("gemini_is_diff", False)


class GeminiRejectDiffCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        diff_file = self.view.settings().get("gemini_diff_file")
        if not diff_file:
            return

        global server
        if server and hasattr(server, "delegate"):
            server.delegate.resolve_diff(diff_file, False)
        else:
            self.view.close()

    def is_visible(self):
        return self.view.settings().get("gemini_is_diff", False)


class GeminiNextChangeCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        regions = self.view.get_regions("gemini_changes")
        if not regions:
            return

        sel = self.view.sel()
        if not sel:
            return

        current_pt = sel[0].begin()
        next_region = None

        # Find first region that starts after current point
        for r in regions:
            if r.begin() > current_pt:
                next_region = r
                break

        # Wrap around
        if not next_region:
            next_region = regions[0]

        self.view.show_at_center(next_region)
        self.view.sel().clear()
        self.view.sel().add(next_region.begin())

    def is_visible(self):
        return self.view.settings().get("gemini_is_diff", False)


class GeminiPrevChangeCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        regions = self.view.get_regions("gemini_changes")
        if not regions:
            return

        sel = self.view.sel()
        if not sel:
            return

        current_pt = sel[0].begin()
        prev_region = None

        # Find last region that starts before current point
        for r in reversed(regions):
            if r.begin() < current_pt:
                prev_region = r
                break

        # Wrap around
        if not prev_region:
            prev_region = regions[-1]

        self.view.show_at_center(prev_region)
        self.view.sel().clear()
        self.view.sel().add(prev_region.begin())

    def is_visible(self):
        return self.view.settings().get("gemini_is_diff", False)
