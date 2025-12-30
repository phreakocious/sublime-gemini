# Sublime Gemini

Sublime Text integration for the [Gemini CLI](https://github.com/google-gemini/gemini-cli). Provides a native-feeling AI assistant right inside your editor.

## Features

- **Integrated Terminal Chat**: Runs the full Gemini CLI TUI inside a dedicated bottom panel in Sublime.
- **Context Aware**: Automatically shares your open files and project structure with the AI (respecting your project roots).
- **Inline Refactoring**: Right-click code to Explain, Refactor, or Add Comments with a live diff preview.
- **Enhanced Diff View**: 
  - **Highlighting**: Proposed changes are highlighted in the buffer.
  - **Smart Navigation**: Automatically scrolls to the first change.
  - **Dual Controls**: "Accept/Reject" phantoms at both the top and bottom of changes for easy access in long files.
- **IDE Companion (MCP Server)**:
  - Enables `gemini fix` and other CLI commands running in external terminals to directly open diffs and navigate files in Sublime Text.
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

## Usage

- **Chat**: Press `Cmd+Shift+G` (macOS) or `Ctrl+Shift+G` (Windows/Linux) to open the Gemini Console at the bottom of your window.
- **Insert Context**: Press `Cmd+Shift+I` (macOS) or `Ctrl+Shift+I` (Windows/Linux) while the Gemini Console is open to insert details about your current selection (file, lines, symbol) into the prompt.
- **Refactor**: Select code, Right Click > **Gemini** > **Inline Edit / Refactor**.
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
    "gemini_path": "gemini" // Path to executable
}
```

## Development Notes

**State**: Active Development / Beta.