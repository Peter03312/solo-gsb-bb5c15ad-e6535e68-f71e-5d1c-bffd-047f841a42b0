"""脱敏核心逻辑的单元测试（仅依赖标准库）。"""

import unittest

from app.redaction import (
    REDACTION_PLACEHOLDER,
    IntervalOutOfBoundsError,
    find_literal_hits,
    find_uncovered_phrase_hits,
    merge_intervals,
    redact_text,
    uncovered_intervals,
    validate_intervals,
)

TEXT = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
# 码点位置：张三=[2,4)  身份证号=[10,28)  电话=[34,45)


class ValidateTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_intervals(len(TEXT), [(2, 4)]), [(2, 4)])

    def test_out_of_bounds(self):
        for bad in [(-1, 2), (0, 0), (3, 2), (10, len(TEXT) + 1)]:
            with self.assertRaises(IntervalOutOfBoundsError):
                validate_intervals(len(TEXT), [bad])

    def test_empty_list_ok(self):
        self.assertEqual(validate_intervals(0, []), [])


class MergeTests(unittest.TestCase):
    def test_overlapping(self):
        self.assertEqual(merge_intervals([(2, 5), (4, 8)]), [(2, 8)])

    def test_adjacent(self):
        self.assertEqual(merge_intervals([(0, 3), (3, 6)]), [(0, 6)])

    def test_unsorted_and_contained(self):
        self.assertEqual(merge_intervals([(10, 20), (0, 5), (12, 15)]), [(0, 5), (10, 20)])

    def test_disjoint_kept(self):
        self.assertEqual(merge_intervals([(0, 2), (4, 6)]), [(0, 2), (4, 6)])


class RedactTests(unittest.TestCase):
    def test_basic(self):
        result = redact_text(TEXT, [(2, 4), (10, 28), (34, 45)])
        self.assertEqual(
            result,
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )

    def test_merge_adjacent_and_overlap(self):
        result = redact_text(TEXT, [(2, 4), (4, 5), (10, 28), (20, 30), (34, 45)])
        self.assertEqual(
            result,
            "患者[已遮蔽]身份证号 [已遮蔽]系电话 [已遮蔽]。",
        )

    def test_placeholder(self):
        self.assertEqual(REDACTION_PLACEHOLDER, "[已遮蔽]")

    def test_empty_intervals_keeps_text(self):
        self.assertEqual(redact_text(TEXT, []), TEXT)

    def test_full_coverage(self):
        self.assertEqual(redact_text(TEXT, [(0, len(TEXT))]), "[已遮蔽]")

    def test_unicode_code_points(self):
        # 🙂 是 BMP 之外的字符，仍按 1 个码点计
        self.assertEqual(redact_text("a🙂bc", [(1, 2)]), "a[已遮蔽]bc")


class LiteralHitTests(unittest.TestCase):
    def test_basic_case_sensitive(self):
        # 区分大小写的字面量命中
        self.assertEqual(find_literal_hits("aBcAbc", ["Abc"]), [(3, 6)])
        self.assertEqual(find_literal_hits("abcABC", ["abc"]), [(0, 3)])

    def test_multiple_non_overlapping(self):
        self.assertEqual(
            find_literal_hits("xx-yy-xx", ["xx"]), [(0, 2), (6, 8)]
        )

    def test_overlapping_hits_are_all_reported(self):
        # 每次命中只前进 1 个码点：重叠命中全部报告
        self.assertEqual(find_literal_hits("aaaa", ["aaa"]), [(0, 3), (1, 4)])

    def test_multiple_phrases_dedup_and_sort(self):
        # 同一区间被两个短语命中时去重，结果按起点排序
        self.assertEqual(
            find_literal_hits("abc", ["abc", "abc", "ab"]),
            [(0, 2), (0, 3)],
        )

    def test_unicode_code_point_positions(self):
        # 🙂 算 1 个码点；位置为 Python 字符串下标
        text = "a🙂秘🙂b"
        self.assertEqual(find_literal_hits(text, ["秘"]), [(2, 3)])
        self.assertEqual(find_literal_hits(text, ["🙂"]), [(1, 2), (3, 4)])

    def test_no_match(self):
        self.assertEqual(find_literal_hits("hello", ["zzz"]), [])

    def test_not_regex(self):
        # 点号按字面量处理，不是正则通配符
        self.assertEqual(find_literal_hits("a.c", ["."]), [(1, 2)])
        self.assertEqual(find_literal_hits("abc", ["a.c"]), [])


class CoverageTests(unittest.TestCase):
    def test_fully_covered_single_interval(self):
        self.assertEqual(uncovered_intervals([(2, 5)], [(2, 5)]), [])
        self.assertEqual(uncovered_intervals([(2, 5)], [(0, 6)]), [])

    def test_partial_coverage_is_uncovered(self):
        # 只覆盖一半、或只覆盖相邻位置都不算完整覆盖
        self.assertEqual(uncovered_intervals([(2, 5)], [(2, 4)]), [(2, 5)])
        self.assertEqual(uncovered_intervals([(2, 5)], [(3, 5)]), [(2, 5)])
        self.assertEqual(uncovered_intervals([(2, 5)], [(5, 8)]), [(2, 5)])

    def test_covered_by_merged_intervals(self):
        # 两个相交/相邻区间合并后完整覆盖命中
        self.assertEqual(uncovered_intervals([(0, 6)], [(0, 3), (3, 6)]), [])
        self.assertEqual(uncovered_intervals([(0, 6)], [(0, 4), (3, 6)]), [])

    def test_mixed_uncovered(self):
        self.assertEqual(
            uncovered_intervals([(0, 3), (10, 13), (20, 23)], [(0, 3), (20, 23)]),
            [(10, 13)],
        )


class PhraseVerificationTests(unittest.TestCase):
    TEXT = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"

    def test_empty_phrases_disables_verification(self):
        # 空清单表示停用核验：即使没有任何遮蔽区间也返回空
        self.assertEqual(find_uncovered_phrase_hits(self.TEXT, [], []), [])

    def test_hit_inside_intervals_is_covered(self):
        # 身份证号在 [10,28) 内：短语命中被完整覆盖
        self.assertEqual(
            find_uncovered_phrase_hits(self.TEXT, ["110101199003077777"], [(10, 28)]),
            [],
        )

    def test_hit_outside_intervals_is_uncovered(self):
        # 姓名未遮蔽：命中 [2,4) 未被覆盖
        hits = find_uncovered_phrase_hits(self.TEXT, ["张三"], [(10, 28)])
        self.assertEqual(hits, [(2, 4)])

    def test_case_sensitive_end_to_end(self):
        self.assertEqual(
            find_uncovered_phrase_hits("令牌ABC有效", ["abc"], []), []
        )
        self.assertEqual(
            find_uncovered_phrase_hits("令牌ABC有效", ["ABC"], []), [(2, 5)]
        )

    def test_only_positions_returned_no_echo(self):
        # 返回值只含码点区间，不含任何短语或原文
        hits = find_uncovered_phrase_hits("x机密y", ["机密"], [])
        self.assertEqual(hits, [(1, 3)])
        self.assertNotIn("机密", repr(hits))


if __name__ == "__main__":
    unittest.main()
