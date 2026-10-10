# Vendored AMap MCP server

`amap_mcp_server_0_1_11.py` is derived from `amap-mcp-server` 0.1.11, released
under the MIT License by sugarforever. The upstream license is preserved in
`LICENSE.amap-mcp-server`.

The project copy adds request timeouts, safe error messages, result provenance,
query pagination and filters, and preservation of provider route details. The
copy retains the upstream tool names and existing argument names so callers
remain compatible. Upstream source distribution: the `amap-mcp-server==0.1.11`
package on PyPI; the runtime dependencies are still supplied by `uvx`.
