"""敏感区间校验、合并与脱敏替换。

区间约定：左闭右开 [start, end)，位置按 Unicode 码点计，
与 Python 字符串的下标语义一致（包括对 BMP 之外的字符）。
"""

REDACTION_PLACEHOLDER = "[已遮蔽]"


class IntervalOutOfBoundsError(ValueError):
    """区间为空、倒置或超出原文长度。"""


def validate_intervals(text_length: int, intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """校验并规范化区间；任何越界区间都会让整次请求被拒绝。"""
    normalized = []
    for start, end in intervals:
        if start < 0 or end > text_length or start >= end:
            raise IntervalOutOfBoundsError(
                f"interval [{start}, {end}) is out of bounds for text length {text_length}"
            )
        normalized.append((start, end))
    return normalized


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相交或相邻的区间，返回按起点排序的不相交区间列表。"""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def redact_text(text: str, intervals: list[tuple[int, int]]) -> str:
    """将合并后的每个区间替换为占位符，其余原文保持不变。"""
    parts: list[str] = []
    cursor = 0
    for start, end in merge_intervals(intervals):
        parts.append(text[cursor:start])
        parts.append(REDACTION_PLACEHOLDER)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def find_literal_hits(text: str, phrases: list[str]) -> list[tuple[int, int]]:
    """按 Unicode 码点查找区分大小写的字面量命中，返回所有命中区间。

    - 纯字面量子串匹配（``str.find``，非正则、不做任何大小写折叠），位置即
      Python 字符串下标（BMP 之外的字符算 1 个码点）；
    - 每次命中后只前进 1 个码点继续查找，因此允许重叠命中（短语为 "aaa"、
      原文为 "aaaa" 时得到 [0,3) 与 [1,4) 两处命中），绝不漏报；
    - 同一区间被多个短语命中时去重，结果按 (start, end) 排序。
    """
    hits: set[tuple[int, int]] = set()
    for phrase in phrases:
        start = 0
        while True:
            index = text.find(phrase, start)
            if index < 0:
                break
            hits.add((index, index + len(phrase)))
            start = index + 1
    return sorted(hits)


def uncovered_intervals(
    hits: list[tuple[int, int]], covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """返回未被遮蔽区间完整覆盖的命中区间（左闭右开，按 Unicode 码点计）。

    遮蔽区间先按相交/相邻合并（与发布时的脱敏合并口径一致）；一个命中区间
    只有整体落在某段遮蔽区间内才算被完整覆盖，部分覆盖也算未覆盖。
    """
    covered_merged = merge_intervals(covered)
    uncovered: list[tuple[int, int]] = []
    for hit_start, hit_end in hits:
        if not any(
            cover_start <= hit_start and hit_end <= cover_end
            for cover_start, cover_end in covered_merged
        ):
            uncovered.append((hit_start, hit_end))
    return uncovered


def find_uncovered_phrase_hits(
    text: str, phrases: list[str], intervals: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """短语核验：返回原文中未被给定遮蔽区间完整覆盖的敏感短语命中位置。

    空清单表示停用核验，调用方不应在停用场景调用本函数（调用也必然返回空）。
    只输出码点区间，绝不回显短语或原文。
    """
    if not phrases:
        return []
    return uncovered_intervals(find_literal_hits(text, phrases), intervals)
