"""工具自定义异常：对应 CLI 退出码。"""


class UsageError(Exception):
    """用法/参数问题 → 退出码 2。"""


class DbcError(Exception):
    """DBC 加载失败 → 退出码 3。"""


class BlfError(Exception):
    """BLF 读取失败 → 退出码 4。"""
