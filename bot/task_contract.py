"""
bot/task_contract.py — 任务节点契约（借鉴 LCA tasks/contract.py）

把"任务类必须具备哪些成员"从"运行时才报错"提前到"注册时即报错"。

任务 = 一个类,类上声明标识信息(name/display_name/category/param_schema),
实现 build_steps(ctx, params) 返回步骤链。新增任务 = 新增文件 + 在 __init__.py
登记,登记时 validate_task_module 立即校验,漏写成员当场抛 TaskContractError,
而不是等挂机跑到一半才 AttributeError。

本步学到的知识点:
- Protocol(结构化类型):约定了类"长得像什么样"而不关心它继承谁,
  @runtime_checkable 让它能配合 isinstance 做运行时检查
- fail-fast:把错误暴露点提前到 import 阶段,是最便宜的防御
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskProtocol(Protocol):
    """任务类需要满足的接口(鸭子类型的"官方盖章")"""

    name: str
    display_name: str
    category: str
    param_schema: dict
    required_templates: list  # [(模板目录, 模板名), ...] —— 任务的模板依赖清单

    def build_steps(self, ctx, params=None): ...


class TaskContractError(TypeError):
    """任务模块不满足契约,注册时抛出"""


def validate_task_module(task_cls) -> None:
    """
    校验一个任务类是否满足契约。不满足抛 TaskContractError,
    报错信息指明具体缺哪一项,方便定位到哪个任务文件。
    """
    label = f"{task_cls.__module__}.{task_cls.__name__}" if task_cls else "?"

    name = getattr(task_cls, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise TaskContractError(f"任务 {label} 缺少非空字符串 'name'")

    display_name = getattr(task_cls, "display_name", None)
    if not isinstance(display_name, str) or not display_name.strip():
        raise TaskContractError(f"任务 '{name}' 缺少非空字符串 'display_name'")

    category = getattr(task_cls, "category", None)
    if not isinstance(category, str) or not category.strip():
        raise TaskContractError(f"任务 '{name}' 缺少非空字符串 'category'")

    schema = getattr(task_cls, "param_schema", None)
    if not isinstance(schema, dict):
        raise TaskContractError(f"任务 '{name}' 的 'param_schema' 必须是 dict")

    # required_templates:声明"这个任务依赖哪些模板"。只校验格式(存在性留给
    # worker 运行前预检 —— 全新环境没采图时 import 阶段崩掉整个程序太激进)。
    # 允许缺失(动态任务如 flow 的图全在节点引用里，用空清单或不写)。
    req = getattr(task_cls, "required_templates", None)
    if req is not None:
        if not isinstance(req, list):
            raise TaskContractError(
                f"任务 '{name}' 的 'required_templates' 必须是 [(目录, 名字), ...] 列表")
        for i, ref in enumerate(req):
            ok = (isinstance(ref, (tuple, list)) and len(ref) == 2
                  and all(isinstance(v, str) for v in ref))
            if not ok:
                raise TaskContractError(
                    f"任务 '{name}' 的 required_templates[{i}] = {ref!r} "
                    f"必须是 (目录字符串, 名字字符串) 二元组")

    build_steps = getattr(task_cls, "build_steps", None)
    if not callable(build_steps):
        raise TaskContractError(f"任务 '{name}' 缺少可调用的 'build_steps(ctx, params)'")


def validate_all(tasks: dict) -> None:
    """校验整个注册表。逐个校验,首个不满足即抛。"""
    for task_name, task_cls in tasks.items():
        validate_task_module(task_cls)