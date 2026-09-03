from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / "skills" / "aivideoeditor-usergrowth-automation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from usergrowth_automation.usergrowth_browser import (  # noqa: E402
    _extract_task_list_total,
    _task_success_state,
)


class TaskPollingTests(unittest.TestCase):
    def test_empty_table_headers_are_not_a_failure(self) -> None:
        body = (
            "任务ID 对象 执行内容 状态 执行时间 总任务数 执行成功数量 "
            "执行失败数量 创建者 操作 暂无数据 共1条"
        )
        self.assertEqual(_task_success_state(body), (False, False))

    def test_explicit_failed_row_is_still_a_failure(self) -> None:
        self.assertEqual(_task_success_state("127030130 状态 失败"), (False, True))
        self.assertEqual(
            _task_success_state("总任务数20 执行成功数量0 执行失败数量20"),
            (False, True),
        )

    def test_requirement_limit_notice_is_a_business_failure(self) -> None:
        self.assertEqual(
            _task_success_state("状态 已达到订单要求的创意单元数量"),
            (False, True),
        )
        self.assertEqual(
            _task_success_state("状态 已达到工单要求的素材数量"),
            (False, True),
        )

    def test_positive_count_is_read_from_empty_table_footer(self) -> None:
        self.assertEqual(_extract_task_list_total("暂无数据 共 20 条"), 20)
        self.assertEqual(_extract_task_list_total("暂无数据 共 0 条"), 0)
        self.assertIsNone(_extract_task_list_total("暂无数据"))


if __name__ == "__main__":
    unittest.main()
