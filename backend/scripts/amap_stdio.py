"""在高德工具模块内隔离调试输出，保留 MCP 的标准输出协议通道。"""


def main():
    from vendor import amap_mcp_server_0_1_11 as server

    def diagnostic_print(*args, **kwargs):
        # 供应商原始响应可能包含路线等数据，既不写 stdout，也不转存 stderr。
        pass

    # 仅覆盖第三方工具模块解析到的 print，不改 builtins 或 MCP 传输输出。
    server.print = diagnostic_print
    server.mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
