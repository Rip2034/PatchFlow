"""测试流水线动画效果 — 在终端直接运行: python tests/manual_test_animation.py"""
import time
import sys

# 确保输出到终端
if not sys.stdout.isatty():
    print("[WARN] stdout is not a TTY! Run this script in a real terminal.")
    print("       Command: python tests/manual_test_animation.py")
    sys.exit(1)


def test_panel_mode():
    """测试 Plan A — Rich Panel 逐行模式"""
    print("\n" + "=" * 60)
    print("  Plan A — Rich Panel 模式")
    print("=" * 60 + "\n")

    from patchflow.utils.agent_display import AgentPipelineDisplay

    d = AgentPipelineDisplay(use_rich=True)
    d.add_step("analyzer", "claude-sonnet-4", "Task: 修复 app.py 中的 NameError")
    d.add_step("fixer", "deepseek-chat", "Files: app.py, utils.py, config.py")
    d.add_step("reviewer", "deepseek-chat")

    d.start()
    time.sleep(0.8)

    d.set_running(0)
    time.sleep(1.5)
    d.set_completed(0, 'Error: NameError — line 42, "user_name" undefined')

    d.set_running(1)
    time.sleep(2.0)
    d.set_completed(1, "2 patches generated — app.py, utils.py")

    d.set_running(2)
    time.sleep(1.5)
    d.set_completed(2, "Score: 8/10 (approved)")

    d.finish(True)
    print("\nPanel 模式测试完成 ✓\n")


def test_live_mode():
    """测试 Plan B — Rich Live 实时仪表盘（带重试）"""
    print("\n" + "=" * 60)
    print("  Plan B — Live 实时仪表盘")
    print("=" * 60)
    print("  (3秒后开始...)")
    print("=" * 60 + "\n")
    time.sleep(3)

    from patchflow.utils.live_dashboard import LivePipelineDashboard

    d = LivePipelineDashboard()
    d.add_step("analyzer", "claude-sonnet-4", "Task: 修复 utils.py 中的 TypeError")
    d.add_step("fixer", "deepseek-chat", "Files: utils.py, handlers.py")
    d.add_step("reviewer", "deepseek-chat")

    d.start()
    time.sleep(0.6)

    d.set_running(0)
    d.set_detail(0, "Scanning code structure...")
    time.sleep(1.2)
    d.set_detail(0, "Identifying root cause...")
    time.sleep(1.2)
    d.set_completed(0, "Error: TypeError in utils.py:87")

    d.set_running(1)
    d.set_detail(1, "Generating patches...")
    time.sleep(1.5)
    d.set_detail(1, "Applying patches...")
    time.sleep(1.0)
    d.set_completed(1, "2 patches generated")

    # 模拟 Reviewer 驳回 → 重试
    d.set_running(2)
    time.sleep(1.8)
    d.set_completed(2, "Score: 6/10 (rejected)")

    d.set_retry(1)
    d.set_detail(1, "Regenerating with review feedback...")
    time.sleep(2.0)
    d.set_completed(1, "3 patches (redo)")

    d.set_retry(2)
    d.set_detail(2, "Re-reviewing...")
    time.sleep(1.2)
    d.set_completed(2, "Score: 9/10 (approved)")

    d.finish(True)
    print("\nLive 模式测试完成 ✓\n")


def test_fallback_mode():
    """测试降级模式 — 非 TTY/CI 环境的纯 logger 输出"""
    print("\n" + "=" * 60)
    print("  降级模式 — Logger 纯文本（模拟 CI 环境）")
    print("=" * 60 + "\n")

    from patchflow.utils.agent_display import AgentPipelineDisplay

    d = AgentPipelineDisplay(use_rich=False)
    d.add_step("analyzer", "deepseek", "Task: fix")
    d.add_step("fixer", "claude", "Files: app.py")
    d.add_step("reviewer", "deepseek")

    d.start()
    d.set_running(0)
    d.set_completed(0, "Error: NameError")
    d.set_running(1)
    d.set_completed(1, "1 patch")
    d.set_running(2)
    d.set_failed(2, "Score: 3/10 — rejected")
    d.finish(False)
    print("\n降级模式测试完成 ✓\n")


def test_diff_coloring():
    """测试 diff 彩色输出"""
    print("\n" + "=" * 60)
    print("  Diff 彩色高亮")
    print("=" * 60 + "\n")

    from patchflow.utils.diff import print_colored_diff

    diff_lines = [
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -40,6 +42,8 @@",
        " def main():",
        "     print('hello')",
        "-    result = user_name + 1",
        "-    return result",
        "+    user_name = 'world'",
        "+    result = user_name + str(1)",
        "+    return result",
        " ",
        " if __name__ == '__main__':",
        "     main()",
    ]
    print_colored_diff(diff_lines)
    print("\nDiff 彩色测试完成 ✓\n")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  PatchFlow 动画效果测试")
    print("=" * 60)

    test_panel_mode()
    test_diff_coloring()
    test_fallback_mode()
    test_live_mode()

    print("=" * 60)
    print("  全部测试完成!")
    print("=" * 60)
