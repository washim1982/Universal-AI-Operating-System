"""The one place AIOS names telemetry attributes (WP 1.6).

The OpenTelemetry GenAI semantic conventions are still in Development status, so their names
are pinned here and changed in one place when they move.
"""

from __future__ import annotations

GENAI_SEMCONV = "GenAI semantic conventions, Development status, as of October 2026"

OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
CONVERSATION_ID = "gen_ai.conversation.id"

OP_CHAT = "chat"
OP_INVOKE_AGENT = "invoke_agent"
OP_EXECUTE_TOOL = "execute_tool"

# AIOS attributes (no convention exists)
TENANT = "aios.tenant"
APP = "aios.app"
RUN_ID = "aios.run.id"
STEP = "aios.step"
CAPABILITY = "aios.capability"
COST_USD = "aios.cost_usd"
EVENT_ID = "aios.event.id"
WORKFLOW = "aios.workflow"
