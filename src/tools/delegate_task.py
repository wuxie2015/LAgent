from ..application.subagent import failure
from ..domain.delegation import SubtaskRequest


class DelegateTaskTool:
    name = 'delegate_task'
    definition = {'type': 'function', 'function': {
        'name': name,
        'description': '自主委派可独立验收的大量阅读、搜索或分析，隔离中间上下文。简单任务直接处理。子任务只读、不可递归；只返回精简结论与证据。必须传递相关用户约束，完成后核验关键证据。',
        'parameters': {'type': 'object', 'properties': {
            'goal': {'type': 'string'}, 'context': {'type': 'string', 'description': '必要背景、相关路径和用户约束；不要复制完整对话或秘密'},
            'expected_output': {'type': 'string', 'description': '输出与验收要求'}},
            'required': ['goal', 'context', 'expected_output']}}}

    def __init__(self, runner):
        self.runner = runner

    def begin_request(self, budget):
        self.runner.begin_request(budget)

    def run(self, goal, context, expected_output):
        try:
            return self.runner.run(SubtaskRequest(goal, context, expected_output))
        except (ValueError, OSError) as exc:
            return failure('delegation_rejected:' + str(exc))
