"""HelloAgents智能旅行助手 - 后端应用"""

import warnings

from authlib.deprecate import AuthlibDeprecationWarning

# HelloAgents 0.2.x 要求 FastMCP <3；FastMCP 2 的 JWT 模块仍使用旧 JOSE。
# 仅处理这个上游导入提示，不屏蔽其他弃用警告或运行异常。
warnings.filterwarnings(
    "ignore",
    message=r"^authlib\.jose module is deprecated, please use joserfc instead\.",
    category=AuthlibDeprecationWarning,
    module=r"^fastmcp\.server\.auth\.providers\.jwt$",
)

__version__ = "1.0.0"
