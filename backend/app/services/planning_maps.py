"""规划工具调用的显式超时；超时退出 MCP 会话，不遗留后台查询线程。"""
import asyncio
from hello_agents.protocols.mcp.client import MCPClient


async def call_with_timeout(source, server_args, env, name, arguments, seconds):
    async with asyncio.timeout(seconds):
        async with MCPClient(source, server_args, env=env) as client:
            return await client.call_tool(name, arguments)


class PlanningMaps:
    def __init__(self, tool, seconds):
        self.tool, self.seconds = tool, seconds

    def get_expanded_tools(self):
        return self.tool.get_expanded_tools()

    def run(self, value):
        return asyncio.run(call_with_timeout(
            self.tool.server if self.tool.server else self.tool.server_command,
            self.tool.server_args, self.tool.env,
            value['tool_name'], value['arguments'], self.seconds))
