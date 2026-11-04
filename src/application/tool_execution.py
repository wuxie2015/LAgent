"""Validate model arguments and bound retries within one user request."""
import inspect
import json
import math
from threading import Lock

from ..common.tool_errors import tool_error


def _validate(value, schema, path="arguments"):
    kind = schema.get("type")
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": type(value) is bool, "null": value is None,
    }
    if kind and not matches.get(kind, False):
        raise ValueError(f"{path} 必须是 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 必须是 {schema['enum']} 中的一项")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems = []
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            problems.append("缺少必填参数：" + ", ".join(sorted(missing)))
        if "properties" in schema and schema.get("additionalProperties") is not True:
            extra = value.keys() - properties.keys()
            if extra:
                problems.append("不支持的参数：" + ", ".join(sorted(extra)))
        if problems:
            raise ValueError("；".join(problems))
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], path + "." + key)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            _validate(item, schema["items"], path + "[]")
    if type(value) in {int, float}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} 小于允许的最小值")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} 超过允许的最大值")


class ToolExecutor:
    def __init__(self, tools):
        self.tools = tools
        self.failures = {}
        self.unavailable = {}
        self.lock = Lock()
        self.in_flight = set()

    def execute(self, name, arguments):
        tool = self.tools[name]
        schema = tool.definition.get("function", {}).get("parameters", {})
        validation = None
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments 必须是JSON对象")
            _validate(arguments, schema)
            inspect.signature(tool.run).bind(**arguments)
        except (ValueError, TypeError) as exc:
            validation = tool_error("invalid_arguments", str(exc), True,
                expected_parameters=schema, hint="按工具定义纠正参数，不添加未声明字段。" + ("读取URL用web_fetch，不要传web_search的id/cursor。" if name == "web_search" else ""))
        key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        with self.lock:
            previous = self.failures.get(key)
            if name in self.unavailable:
                return tool_error("repeat_limit", "该工具缺少配置，改变查询参数也不能解决；请说明限制或改用其他可用来源。", previous_error_code=self.unavailable[name])
            if key in self.in_flight:
                return tool_error("duplicate_in_flight", "同一调用正在执行，已阻止并发重复执行。")
            if previous and (not previous["retryable"] or previous["count"] >= (2 if previous["code"] == "network_error" else 1)):
                return tool_error("repeat_limit", "此调用已失败，停止重复相同参数；纠正参数、改用其他来源，或整理已获得的结果。",
                                  previous_error_code=previous["code"])
            self.in_flight.add(key)
        try:
            if validation:
                result = validation
            else:
                try:
                    result = tool.run(**arguments)
                except Exception as exc:
                    result = tool_error("tool_exception", "工具执行失败：" + type(exc).__name__)
            if not isinstance(result, dict):
                result = tool_error("invalid_result", "工具未返回约定的JSON对象。")
            if result.get("error") or result.get("timed_out") or result.get("exit_code", 0) != 0:
                code = result.get("error_code", "tool_failed")
                with self.lock:
                    if code == "not_configured":
                        self.unavailable[name] = code
                    self.failures[key] = {"code": code, "retryable": result.get("retryable", False),
                                          "count": (previous["count"] if previous else 0) + 1}
            else:
                with self.lock:
                    self.failures.pop(key, None)
            return result
        finally:
            with self.lock:
                self.in_flight.discard(key)
