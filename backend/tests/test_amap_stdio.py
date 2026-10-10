"""验证项目内 vendored AMap MCP 服务的启动、工具契约与 stdio 协议。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from app.services import amap_service


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "backend" / "scripts" / "amap_stdio.py"
SCRIPTS = ROOT / "backend" / "scripts"
EXPECTED_TOOLS = {
    "maps_regeocode", "maps_geo", "maps_ip_location", "maps_weather",
    "maps_bicycling_by_address", "maps_bicycling_by_coordinates",
    "maps_direction_walking_by_address", "maps_direction_walking_by_coordinates",
    "maps_direction_driving_by_address", "maps_direction_driving_by_coordinates",
    "maps_direction_transit_integrated_by_address", "maps_direction_transit_integrated_by_coordinates",
    "maps_distance", "maps_text_search", "maps_around_search", "maps_search_detail",
}


class AmapStdioTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def server_command(self):
        class Tool:
            def __init__(self, **kwargs):
                self.server_command = kwargs["server_command"]
                self.env = kwargs["env"]
                self._available_tools = ["maps_text_search"]
        settings = SimpleNamespace(amap_api_key="fixture-key", planner_tool_timeout_seconds=7)
        with patch.object(amap_service, "_amap_mcp_tool", None), \
             patch.object(amap_service, "get_settings", return_value=settings), \
             patch.object(amap_service.shutil, "which", return_value="/fixture/uvx"), \
             patch.object(amap_service, "MCPTool", Tool):
            tool = amap_service.get_amap_mcp_tool()
        self.assertIn("AMAP_HTTP_TIMEOUT_SECONDS", tool.env)
        self.assertEqual(tool.env["AMAP_HTTP_TIMEOUT_SECONDS"], "7")
        return tool.server_command

    def entry_args(self):
        command = self.server_command()
        self.assertTrue(command[command.index("--from") + 1].startswith("amap-mcp-server=="))
        self.assertEqual(command[command.index("--with") + 1], "pydantic==2.13.5")
        script_path = Path(command[command.index("python") + 1])
        self.assertEqual(script_path.resolve(), ENTRYPOINT.resolve())
        return [str(script_path)]

    def exchange(self, calls=(), *, fixture_http=False):
        if fixture_http:
            bootstrap = self.root / "bootstrap.py"
            bootstrap.write_text(
                """import runpy
from vendor import amap_mcp_server_0_1_11 as server
class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload
def fake_get(url, **kwargs):
    params = kwargs.get('params', {})
    if params.get('keywords') == 'fixture-failure':
        import requests
        raise requests.ConnectionError('fixture-key https://example.invalid/?key=fixture-key')
    server.print({'status': '1', 'private_marker': 'fixture-raw-route'})
    return Response({'status': '1', 'count': '1', 'pois': [
        {'id': 'fixture-poi', 'name': 'Fixture place', 'location': '121.5,31.2'}
    ]})
server.requests.get = fake_get
runpy.run_path(%r, run_name='__main__')
""" % str(ENTRYPOINT)
            )
            args = [str(bootstrap)]
        else:
            args = self.entry_args()

        async def run():
            stderr_path = self.root / "stderr.log"
            with stderr_path.open("w+") as err:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=args,
                    env={**os.environ, "AMAP_MAPS_API_KEY": "fixture-key", "PYTHONPATH": str(SCRIPTS)},
                )
                async with asyncio.timeout(15):
                    async with stdio_client(params, errlog=err) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            results = []
                            for name, arguments in calls:
                                results.append(await session.call_tool(name, arguments))
                err.seek(0)
                return listed, results, err.read()
        return asyncio.run(run())

    def test_vendored_entrypoint_discovers_all_legacy_tools_and_arguments(self):
        listed, _, stderr = self.exchange()
        self.assertEqual({tool.name for tool in listed.tools}, EXPECTED_TOOLS)
        schemas = {tool.name: tool.inputSchema for tool in listed.tools}
        props = {name: set(schema.get("properties", {})) for name, schema in schemas.items()}
        self.assertTrue({"keywords", "city", "citylimit"}.issubset(props["maps_text_search"]))
        self.assertTrue({"location", "radius", "keywords"}.issubset(props["maps_around_search"]))
        self.assertIn("id", props["maps_search_detail"])
        self.assertIn("city", props["maps_weather"])
        self.assertTrue({"origin", "destination", "city", "cityd"}.issubset(props["maps_direction_transit_integrated_by_coordinates"]))
        self.assertTrue({"date", "time", "strategy"}.issubset(props["maps_direction_transit_integrated_by_coordinates"]))
        self.assertNotIn("fixture-key", stderr)
        self.assertNotIn("fixture-raw-route", stderr)

    def test_actual_stdio_call_failure_does_not_leak_and_next_call_succeeds(self):
        with patch("mcp.client.stdio.logger.exception") as protocol_error:
            listed, results, stderr = self.exchange([
                ("maps_text_search", {"keywords": "fixture-failure", "city": "上海"}),
                ("maps_text_search", {"keywords": "fixture-success", "city": "上海"}),
            ], fixture_http=True)
        self.assertEqual({tool.name for tool in listed.tools}, EXPECTED_TOOLS)
        self.assertEqual([result.isError for result in results], [False, False])
        first = json.loads(results[0].content[0].text)
        second = json.loads(results[1].content[0].text)
        self.assertEqual(first["result_status"], "error")
        self.assertEqual(first["result_error"]["category"], "network")
        self.assertEqual(second["result_status"], "ok")
        self.assertNotIn("fixture-key", results[0].content[0].text)
        self.assertNotIn("example.invalid", results[0].content[0].text)
        self.assertNotIn("fixture-key", stderr)
        self.assertNotIn("example.invalid", stderr)
        self.assertNotIn("fixture-raw-route", stderr)
        protocol_error.assert_not_called()

    def test_startup_failure_exits_nonzero_without_stdout_protocol_noise(self):
        import subprocess
        env = {key: value for key, value in os.environ.items() if key != "AMAP_MAPS_API_KEY"}
        env["PYTHONPATH"] = str(SCRIPTS)
        process = subprocess.run(
            [sys.executable, str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, "")
        self.assertIn("AMAP_MAPS_API_KEY environment variable is required", process.stderr)

    def test_local_runner_uses_vendored_source_and_bounded_timeout(self):
        command = self.server_command()
        self.assertIn(str(ENTRYPOINT), command)
        self.assertTrue((SCRIPTS / "vendor" / "amap_mcp_server_0_1_11.py").is_file())


if __name__ == "__main__":
    unittest.main()
