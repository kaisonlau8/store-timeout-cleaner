"""列表页共用的战区 + 门店搜索。不改统计口径，只决定一行要不要显示。"""

from __future__ import annotations


def store_query_match(name: str, code: str, query: str) -> bool:
    """门店名称或编码的大小写不敏感子串匹配。空查询匹配全部。"""
    needle = (query or "").strip().casefold()
    if not needle:
        return True
    if needle in (name or "").casefold():
        return True
    return bool((code or "").strip()) and needle in str(code).casefold()


def row_visible(row: dict, theater: str, query: str) -> bool:
    """战区精确匹配，且门店名称/编码子串匹配。两个条件同时生效。"""
    selected = (theater or "").strip()
    if selected and (row.get("theater") or "") != selected:
        return False
    return store_query_match(
        row.get("name") or row.get("store") or "",
        row.get("code") or "",
        query,
    )
