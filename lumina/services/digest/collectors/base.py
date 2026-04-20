"""
lumina/digest/collectors/base.py — Collector Protocol

定义所有 collector 函数必须满足的接口。
约定：函数名以 collect_ 开头，无参数，返回 str。
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class Collector(Protocol):
    """Collector 函数接口：无参数调用，返回 str。

    函数天然满足此 Protocol（callable + __name__ 属性），
    无需显式继承。新增数据源只需在 collectors/ 下建文件，
    写一个 collect_xxx() -> str 函数，即可被自动发现。
    """

    __name__: str

    def __call__(self) -> str: ...
