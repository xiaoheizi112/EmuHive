"""
bot/tasks/ — 任务插件注册表（第 7 步）

任务插件的约定（第 8 步会完整落地）：
每个任务模块导出一个任务类，类上声明元信息（标识名/显示名/分类），
实现 build_steps(ctx, params) 返回步骤链。
新增任务 = 新增一个文件 + 在这里登记，框架零改动。
"""

from bot.tasks.flow import FlowTask

# 任务注册表：任务标识 → 任务类
# ⚠ 「流程即任务」改版：内置脚本任务（login/story/background/daily）已全部删除，
#   唯一的任务就是 flow —— 你在流程编辑器里编的那条流程，就是挂机的任务本身。
#   需要找回某个写死的旧任务时，从 git 历史恢复对应文件并在此登记即可。
TASKS = {
    FlowTask.name: FlowTask,
}

# 契约校验：任务类漏写成员在此处 fail-fast（import 即报错），
# 而不是等挂机跑到一半才 AttributeError
from bot.task_contract import validate_all

validate_all(TASKS)


def get_task(name: str):
    return TASKS.get(name)
