import os
import json
import glob
import tempfile
import sys


def get_connection_info():
    tmp = tempfile.gettempdir()
    # Pattern used by gemini.py: gemini-ide-server-{pid}-{port}.json
    pattern = os.path.join(tmp, "gemini", "ide", "gemini-ide-server-*.json")
    files = glob.glob(pattern)

    if not files:
        print(f"No discovery files found in {pattern}")
        return None, None

    # Sort by modification time, newest first
    files.sort(key=os.path.getmtime, reverse=True)
    latest_file = files[0]

    print(f"Found latest discovery file: {latest_file}")

    try:
        with open(latest_file, "r") as f:
            data = json.load(f)
            return data.get("port"), data.get("authToken")
    except Exception as e:
        print(f"Error reading {latest_file}: {e}")
        return None, None


def main():
    port, token = get_connection_info()
    if not port or not token:
        print("Could not retrieve connection info.")
        sys.exit(1)

    base_url = f"http://127.0.0.1:{port}/mcp"

    print("\n--- Connection Info ---")
    print(f"Port: {port}")
    print(f"Token: {token}")

    print("\n--- 1. SSE Connection (Keep Open) ---")
    print(f'curl -N -H "Authorization: {token}" "{base_url}" ')

    print("\n--- 2. Initialize (JSON-RPC) ---")
    init_payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "curl-test", "version": "1.0"},
        },
        "id": 1,
    }
    # Escaping for shell
    init_json = json.dumps(init_payload).replace('"', '"')
    print(
        f'curl -X POST -H "Authorization: {token}" -H "Content-Type: application/json" -d "{init_json}" "{base_url}" '
    )

    print("\n--- 3. List Tools (JSON-RPC) ---")
    list_payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2}
    list_json = json.dumps(list_payload).replace('"', '"')
    print(
        f'curl -X POST -H "Authorization: {token}" -H "Content-Type: application/json" -d "{list_json}" "{base_url}" '
    )

    print("\n--- 4. Call Tool: navigateTo (JSON-RPC) ---")
    nav_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "navigateTo",
            "arguments": {"filePath": __file__, "line": 1, "character": 1},
        },
        "id": 3,
    }
    nav_json = json.dumps(nav_payload).replace('"', '"')
    print(
        f'curl -X POST -H "Authorization: {token}" -H "Content-Type: application/json" -d "{nav_json}" "{base_url}" '
    )


if __name__ == "__main__":
    main()
