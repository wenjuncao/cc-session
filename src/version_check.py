# -*- coding: utf-8 -*-
"""
version_check.py —— 从 GitHub Releases 获取最新版本号，判断本地是否过低（优化项 #3）。

check(current_version) → (need_update: bool, latest: str, html_url: str)
  GET https://api.github.com/repos/wenjuncao/cc-session/releases/latest
  取 tag_name（去掉前导 v）按 "." 分段与本地版本数值比较；latest > current 即需更新。
纯 urllib + json，短超时；任何网络/解析异常都吞掉并返回 (False, "", "")——离线时不打扰用户。
"""
import json
import urllib.request

_API = "https://api.github.com/repos/wenjuncao/cc-session/releases/latest"
_TIMEOUT = 4


def _parse(v):
    """'v0.0.5' / '0.0.5' → (0, 0, 5)；非数字段记 0，容错。"""
    v = (v or "").strip().lstrip("vV")
    out = []
    for seg in v.split("."):
        try:
            out.append(int(seg))
        except ValueError:
            out.append(0)
    return tuple(out)


def _newer(latest, current):
    """latest 是否严格高于 current（按段数值比较，短段补 0）。"""
    a, b = _parse(latest), _parse(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def check(current_version):
    """返回 (need_update, latest_tag, release_html_url)；异常/离线一律 (False, "", "")。"""
    try:
        req = urllib.request.Request(_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "cc-session-version-check",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        latest = (data.get("tag_name") or "").strip()
        html_url = data.get("html_url") or ""
        if latest and _newer(latest, current_version):
            return True, latest, html_url
        return False, latest, html_url
    except Exception:
        return False, "", ""
