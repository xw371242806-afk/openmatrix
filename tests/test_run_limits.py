import unittest

from main import BoundedRunLimits


class RunLimitsContractTests(unittest.TestCase):
    def setUp(self):
        self.limits = BoundedRunLimits()

    def test_bounded_run_limits_defaults(self):
        self.assertEqual(3, self.limits.max_issues)
        self.assertEqual(1, self.limits.max_concurrent_writes)
        self.assertEqual(2, self.limits.max_attempts_per_issue)
        self.assertEqual(24, self.limits.max_total_hours)
        self.assertEqual(
            {"PR_OPENED", "BLOCKED", "ABORTED"},
            self.limits.terminal_states,
        )

    def test_plan_within_limits_is_valid(self):
        self.assertTrue(self.limits.is_valid_plan(num_issues=1))
        self.assertTrue(self.limits.is_valid_plan(num_issues=3, attempts_per_issue=2))
        self.assertEqual([], self.limits.validate_plan(num_issues=3, attempts_per_issue=2))

    def test_plan_exceeding_issue_cap_is_invalid(self):
        violations = self.limits.validate_plan(num_issues=4)

        self.assertIn("num_issues=4 exceeds max_issues=3", violations)
        self.assertFalse(self.limits.is_valid_plan(num_issues=4))

    def test_plan_exceeding_attempt_cap_is_invalid(self):
        violations = self.limits.validate_plan(num_issues=1, attempts_per_issue=3)

        self.assertIn("attempts_per_issue=3 exceeds max_attempts_per_issue=2", violations)
        self.assertFalse(self.limits.is_valid_plan(num_issues=1, attempts_per_issue=3))

    def test_negative_issue_count_is_invalid(self):
        violations = self.limits.validate_plan(num_issues=-1)

        self.assertIn("num_issues must be non-negative", violations)

    def test_zero_or_fewer_attempts_is_invalid(self):
        violations = self.limits.validate_plan(num_issues=1, attempts_per_issue=0)

        self.assertIn("attempts_per_issue must be >= 1", violations)

    def test_terminal_states_are_recognized(self):
        for state in ("PR_OPENED", "BLOCKED", "ABORTED"):
            self.assertTrue(self.limits.is_terminal_state(state), state)
        self.assertFalse(self.limits.is_terminal_state("IN_PROGRESS"))

    def test_custom_attempt_cap_is_honored(self):
        tight = BoundedRunLimits(max_attempts_per_issue=1)

        self.assertFalse(tight.is_valid_plan(num_issues=1, attempts_per_issue=2))
        self.assertTrue(tight.is_valid_plan(num_issues=1, attempts_per_issue=1))

    def test_full_issue_queue_at_limit_is_valid(self):
        plan = {"issues": [f"issue-{i}" for i in range(self.limits.max_issues)]}
        self.assertLessEqual(len(plan["issues"]), self.limits.max_issues)
        self.assertTrue(self.limits.is_valid_plan(num_issues=len(plan["issues"])))


if __name__ == "__main__":
    unittest.main()
