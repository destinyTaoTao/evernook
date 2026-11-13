from __future__ import annotations

import unittest

from evernook.preference_statements import assess_preferences


class PreferenceStatementTests(unittest.TestCase):
    def test_paragraph_preserves_exact_clause_coordinates(self) -> None:
        text = "今天聊了很久。说到日常交流，我偏好分点回答。这个习惯以后也适用。"
        result = assess_preferences(text)
        self.assertEqual(len(result.statements), 1)
        statement = result.statements[0]
        self.assertEqual(statement.text, "我偏好分点回答。")
        self.assertEqual(text[statement.start:statement.end], statement.text)
        self.assertEqual((statement.category, statement.scope), ("response_style", "long_term"))

    def test_general_preference_and_turn_exception_are_separate(self) -> None:
        result = assess_preferences("我一般喜欢短回答，但这次我希望详细回答。")
        self.assertEqual([(s.scope, s.normalized) for s in result.statements],
                         [("long_term", "我喜欢简短回答"), ("turn", "我希望详细回答。")])
        result = assess_preferences("我偏好详细回答，只对这次有效。")
        self.assertEqual(result.statements[0].scope, "turn")

    def test_quotation_conditional_and_inferred_words_never_become_self_preference(self) -> None:
        for text in ("同事原话是我喜欢摄影。", "如果我喜欢摄影，才会买相机。", "有人推测我喜欢摄影。", "示例：我喜欢摄影。",
                     "请测试我喜欢摄影，不要记住。", "我喜欢摄影，这不是真的。"):
            with self.subTest(text=text):
                result = assess_preferences(text)
                self.assertFalse(result.statements)
                self.assertIsNotNone(result.blocked_reason)

    def test_explicit_negative_correction_is_distinct_from_ordinary_conflict(self) -> None:
        result = assess_preferences("上次说喜欢摄影只是附和别人，现在确认我并不喜欢摄影。")
        self.assertEqual(result.statements[0].scope, "withdrawal")
        self.assertTrue(result.statements[0].negative)
        self.assertEqual(assess_preferences("我不喜欢摄影。").statements[0].scope, "long_term")

    def test_past_and_present_do_not_silently_share_the_same_scope(self) -> None:
        result = assess_preferences("以前我偏好简短回答，但现在我偏好详细回答。")
        self.assertEqual([s.scope for s in result.statements], ["historical", "long_term"])
        self.assertEqual(assess_preferences('以前，我喜欢摄影。').statements[0].scope, 'historical')
        self.assertEqual(assess_preferences('明年开始，我喜欢摄影。').statements[0].scope, 'planned')

    def test_repeated_statements_remain_one_event_and_never_drop_a_late_denial(self) -> None:
        result = assess_preferences("我喜欢摄影。" * 40 + "我不喜欢摄影。")
        self.assertEqual(result.blocked_reason, "too_many_statements")
        self.assertFalse(result.statements)

    def test_questions_and_mixed_high_risk_context_do_not_publish_a_positive_substring(self) -> None:
        self.assertFalse(assess_preferences('我喜欢摄影？').statements)
        self.assertEqual(assess_preferences('我喜欢摄影，我有病史。').blocked_reason, 'high_risk_context')
        self.assertFalse(assess_preferences('朋友说我喜欢摄影。').statements)
        self.assertFalse(assess_preferences('大家认定我喜欢摄影。').statements)
        self.assertFalse(assess_preferences('房东告诉：我喜欢摄影。').statements)


if __name__ == "__main__":
    unittest.main()
