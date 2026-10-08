"""通过实际子进程和 MCP 协议验证高德启动适配，不调用外部服务。"""
import asyncio
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


class AmapStdioTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        package = self.root / 'amap_mcp_server'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / '__main__.py').write_text('from .server import mcp\nmcp.run(transport="stdio")\n')
        (package / 'server.py').write_text('''
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fixture-amap")
@mcp.tool()
def transit():
    print({'status': '1', 'private_marker': 'fixture-raw-route'}, flush=True)
    return {'duration': 600, 'status': '1'}
@mcp.tool()
def fail():
    raise ValueError('fixture-tool-failure')
''')

    def server_command(self):
        class Tool:
            def __init__(self, **kwargs):
                self.server_command = kwargs['server_command']
                self._available_tools = ['transit']
        with patch.object(amap_service, '_amap_mcp_tool', None), \
             patch.object(amap_service, 'get_settings', return_value=SimpleNamespace(amap_api_key='fixture-key')), \
             patch.object(amap_service.shutil, 'which', return_value='/fixture/uvx'), \
             patch.object(amap_service, 'MCPTool', Tool):
            command = amap_service.get_amap_mcp_tool().server_command
        return command

    def command(self):
        command = self.server_command()
        # Replace only the package runner boundary; execute the selected entrypoint.
        if command[1:] == ['amap-mcp-server']:
            return ['-m', 'amap_mcp_server']
        return command[command.index('python') + 1:]

    def exchange(self, calls=('transit',)):
        args = self.command()
        async def run():
            with (self.root / 'stderr.log').open('w+') as err:
                params = StdioServerParameters(command=sys.executable, args=args,
                    env={**os.environ, 'PYTHONPATH': str(self.root)})
                async with asyncio.timeout(10):
                    async with stdio_client(params, errlog=err) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            results = [await session.call_tool(name, {}) for name in calls]
                err.seek(0)
                return listed, results, err.read()
        return asyncio.run(run())

    def test_debug_print_does_not_corrupt_tool_response(self):
        with patch('mcp.client.stdio.logger.exception') as parse_error:
            _, results, _ = self.exchange()
        self.assertFalse(results[0].isError)
        self.assertIn('600', results[0].content[0].text)
        parse_error.assert_not_called()

    def test_raw_debug_response_is_not_copied_to_stderr(self):
        _, results, stderr = self.exchange()
        self.assertFalse(results[0].isError)
        self.assertNotIn('fixture-raw-route', stderr)

    def test_discovery_and_calls_survive_a_tool_failure(self):
        with patch('mcp.client.stdio.logger.exception') as parse_error:
            listed, results, stderr = self.exchange(('transit', 'fail', 'transit'))
        self.assertEqual({tool.name for tool in listed.tools}, {'transit', 'fail'})
        self.assertEqual([result.isError for result in results], [False, True, False])
        self.assertIn('fixture-tool-failure', results[1].content[0].text)
        self.assertIn('600', results[2].content[0].text)
        self.assertNotIn('fixture-raw-route', stderr)
        parse_error.assert_not_called()

    def test_startup_failure_exits_nonzero_and_keeps_error_visible(self):
        import subprocess
        (self.root / 'amap_mcp_server' / 'server.py').write_text(
            "raise RuntimeError('fixture-startup-failure')\n")
        process = subprocess.run([sys.executable, *self.command()],
            env={**os.environ, 'PYTHONPATH': str(self.root)},
            capture_output=True, text=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, '')
        self.assertIn('fixture-startup-failure', process.stderr)

    def test_uvx_environment_pins_the_compatible_server_and_pydantic(self):
        command = self.server_command()
        self.assertIn('--with', command)
        self.assertEqual(command[command.index('--with') + 1], 'pydantic==2.13.5')
        self.assertEqual(command[command.index('--from') + 1], 'amap-mcp-server==0.1.11')
