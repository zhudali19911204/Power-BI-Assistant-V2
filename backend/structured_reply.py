"""Decode a single, unambiguous JSON reply and report safe validation paths."""

import json
import re


class ReplyFormatError(ValueError):
    def __init__(self, issues):
        self.issues = issues[:5]
        super().__init__("；".join(f"{item['path']}：{item['message']}" for item in self.issues))


def issue(path, code, message):
    return ReplyFormatError([{"path": path, "type": code, "message": message}])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise issue("$", "duplicate_key", "JSON 对象含有重复字段，请只保留一个明确的值")
        result[key] = value
    return result


def _invalid_constant(value):
    raise issue("$", "json_constant", "JSON 不能包含 NaN 或 Infinity")


def decode_reply_object(text):
    # Only remove an outer wrapper. Never extract arbitrary braces from prose, coerce
    # business values, repair quotes inside DAX, or accept the last duplicate key.
    text = text.strip().lstrip("\ufeff").strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text, re.I)
    source = (fence[1] if fence else text).strip().lstrip("\ufeff").strip()
    try:
        return json.loads(source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except ReplyFormatError:
        raise
    except json.JSONDecodeError as exc:
        raise issue("$", "json_invalid", f"JSON 语法不合法（第 {exc.lineno} 行，第 {exc.colno} 列）") from exc
    except ValueError as exc:
        raise issue("$", "json_invalid", "JSON 含有无法解析的值，请使用规定的字段和类型") from exc
    except RecursionError as exc:
        raise issue("$", "json_depth", "JSON 嵌套过深，请使用规定的对象结构") from exc
