# Sublime Gemini

Sublime Text integration for the [Gemini CLI](https://github.com/google-gemini/gemini-cli). Provides a native-feeling AI assistant right inside your editor.

## Features

- **Open Editor File Context**: Gemini CLI gains awareness of the files you have open in your editor, providing it with a richer understanding of your project's structure and content.
- **Selection Context**: Gemini CLI can easily access your cursor's position and selected text within the editor, giving it valuable context directly from your current work.
- **Native Diffing**: Seamlessly view, modify, and accept code changes suggested by Gemini CLI directly within the editor. Proposed changes are highlighted in the buffer with smart navigation and dual "Accept/Reject" controls for easy access in long files.
- **Launch Gemini CLI**: Quickly start a new Gemini CLI session directly inside Sublime Text (configurable as a panel, tab, or split) from the Command Palette or via keybindings.
- **IDE Companion (MCP Server)**: Enables `gemini fix` and other CLI commands running in external terminals to directly interact with Sublime Text.
- **Zero-Config Auth**: Automatically manages connection details and pre-authorizes IDE tools (`openDiff`, `closeDiff`, `navigateTo`) for a seamless experience.

## Requirements

- **Sublime Text 4** (Build 4000+)
- **Terminus** package (Install via Package Control)
- **Gemini CLI** (`npm install -g @google/gemini-cli`)

## Installation

1.  Clone this repository into your Sublime Text `Packages` directory:
    ```bash
    cd "$HOME/Library/Application Support/Sublime Text/Packages/"
    git clone https://github.com/yourusername/gemini-cli-sublime.git Gemini
    ```
2.  **Restart Sublime Text**.
3.  **Verify**: Open the Command Palette (`Cmd+Shift+P` or `Ctrl+Shift+P`) and type `Gemini: Open Chat` to confirm the plugin is loaded.

## Usage

- **Chat**: Press `Cmd+Shift+G` (macOS) or `Ctrl+Shift+G` (Windows/Linux) to open the Gemini Console.
- **Insert Context**: Press `Cmd+Shift+I` (macOS) or `Ctrl+Shift+I` (Windows/Linux) while the Gemini Console is open to insert details about your current selection (file, lines, symbol) into the prompt.
- **Refactor**: Select code, Right Click > **Gemini** > **Inline Edit / Refactor**.
- **Stop Generation**: Open the Command Palette and select **Gemini: Stop Generation** to interrupt Gemini (sends Ctrl+C).
- **Commands**: Open Command Palette (`Cmd+Shift+P`) and type `Gemini`.
- **External Terminal**: Just run `gemini` commands in your system terminal. They will automatically detect the running Sublime Text instance for rich interactions.

## How it Works: IDE Integration & Auth

Sublime Gemini runs a local MCP (Model Context Protocol) server. When you launch the chat, it uses a few tricks to ensure a seamless connection:

1.  **Discovery**: It writes discovery files in your system's temporary directory containing the server's port and a unique authentication token.
2.  **Pre-Authorization**: The plugin injects a temporary configuration into the Gemini CLI that whitelists IDE-specific tools, bypassing repetitive permission prompts.
3.  **Dynamic Launcher**: To handle terminal session restoration (where environment variables might be stale), the plugin uses a cross-platform Node.js launcher script. This script dynamically looks up the latest server credentials every time you run the CLI, ensuring a reliable connection even after restarting Sublime.

## Configuration

Settings are available in `Preferences > Package Settings > Gemini`.

```json
{
    "gemini_path": "gemini", // Path to executable
    "view_location": "split", // Where to open chat: "split" (default), "tab", "panel"
    "environment": {
        "GOOGLE_CLOUD_PROJECT": "your-project-id"
    }
}
```

## Troubleshooting

- **Gemini command not found**: Ensure `npm install -g @google/gemini-cli` was run successfully and the `gemini` executable is in your PATH. You can specify the absolute path in `Gemini.sublime-settings`.
- **Connection Refused**: If the chat panel opens but says "Connecting...", try restarting Sublime Text to refresh the MCP server discovery files.
- **Terminus Issues**: Ensure the `Terminus` package is installed and up to date.

## Development Notes

**State**: Active Development / Beta.

## Development Tools

### MCP Standalone Test Client

Included in `scripts/test_mcp_standalone.py` is a Python script to verify the MCP server status and interact with it directly without the full Gemini CLI.

**Usage:**

```bash
# Make sure you are in the project root
python3 scripts/test_mcp_standalone.py [command]
```

**Commands:**

- `info`: Print connection details (Port, Token, Base URL).
- `list`: List all tools exposed by the Sublime MCP server (`openDiff`, `closeDiff`, `navigateTo`).
- `call <tool_name> [json_args]`: Execute a tool.
  - Use `--arg-file KEY=PATH` to load large content from files.

**Examples:**

1.  **Navigate to a file:**
    ```bash
    python3 scripts/test_mcp_standalone.py call navigateTo '{"filePath": "gemini.py", "line": 10, "character": 5}'
    ```

2.  **Open a Diff:**
    ```bash
    # Propose changes to gemini.py using content from a local file
    python3 scripts/test_mcp_standalone.py call openDiff \
      --arg-file newContent=gemini.py \
      '{"filePath": "gemini.py", "explanation": "Testing standalone diff"}'
    ```

3.  **Raw JSON-RPC:**
    ```bash
    python3 scripts/test_mcp_standalone.py raw initialize '{"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}'
    ```

This script automatically locates the active server connection file created by Sublime Text in your system's temp directory.
