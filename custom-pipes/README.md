# Custom Pipes

Custom Open WebUI pipe functions for agent integrations.

These files are version-controlled here for reference. They are **not** loaded
from disk at runtime — install them via the Admin UI:

1. Go to **Admin > Functions > Add Function**
2. Set type to **Pipe**
3. Paste the contents of the desired `.py` file
4. Save and enable

Alternatively, use the `/api/v1/functions/load/url` endpoint to load directly
from a raw GitHub URL.

## Available Pipes

### `deepagent/deepagent_pipe.py`
Connects to the agent-bridge service (OpenAI-compatible proxy). Simplest setup.

### `deepagent/deepagent_direct_pipe.py`
Connects directly to the LangGraph agent server. Configurable reasoning trace
and tool call display via Valves.
