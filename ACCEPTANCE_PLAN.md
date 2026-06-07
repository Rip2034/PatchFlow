
```bash
patchflow --help
# 预期: 看到 15 个命令，包括新增的 search/research/cron/memory/workflow
```

### 2. Workflow 编排引擎

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| Judge Panel | `patchflow workflow judge-panel "检查项目代码质量" -g 2 -j 1` | 多个方案 → 评分 → 选最优 |
| Adversarial Verify | `patchflow workflow verify-claim "print('hello') 是正确的语法" -s 2` | 2 个质疑者投票 |
| Multi-Sweep | `patchflow workflow multi-sweep "审查代码"` | 从 4 个角度分析，按视角分组展示 |
| Parallel 编 | `python -c "from patchflow.core.workflow import parallel; print(parallel([lambda:1,lambda:2,lambda:3]))"` | `[1, 2, 3]` |
| Pipeline 编 | `python -c "from patchflow.core.workflow import pipeline; print(pipeline([1,2], lambda x:x*2, lambda x:x+1))"` | `[[2,3],[4,5]]` |

### 3. Task Manager

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| 基本流程 | 见下方代码片段 A | 3 步顺序执行，依赖解析正确 |

**代码片段 A — TaskManager 验收:**

```python
from patchflow.core.task_manager import TaskManager, create_task_chain

tm = TaskManager()
tasks = create_task_chain(tm, [
    ("Step 1: Scan", "Scan files", ["app.py"]),
    ("Step 2: Fix", "Apply fixes", ["app.py"]),
    ("Step 3: Test", "Run tests", []),
])

# Step 2 应该被 Step 1 阻塞
t2 = tm.get(tasks[1].id)
assert tasks[0].id in t2.blocked_by, "Dependency not set!"

# 执行
def executor(task):
    print(f"  Executing: {task.subject}")
    return True

results = tm.execute_all(executor, parallel=False)
assert len(results) == 3 and all(results.values()), "Not all tasks completed"
print("TaskManager: PASSED")
```

### 4. Memory 系统

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| 添加记忆 | `patchflow memory add project-style "使用 4 空格缩进"` | 显示 "记忆已存储" |
| 搜索记忆 | `patchflow memory recall "缩进"` | 找到 project-style |
| 列出记忆 | `patchflow memory list` | 显示所有记忆 |
| 按类型列出 | `patchflow memory list -t project` | 只显示 project 类型 |
| 删除记忆 | `patchflow memory forget project-style` | 显示 "记忆已删除" |
| 持久化 | 添加记忆 → 重新 `memory list` | 记忆仍然存在（从 .patchflow/memory/ 加载） |

### 5. Cron Scheduler

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| 添加任务 | `patchflow cron add "*/5 * * * *" "analyze"` | 显示任务 ID |
| 添加一次性 | `patchflow cron add "0 12 * * *" "release check" -o` | 非 recurring 任务 |
| 添加持久化 | `patchflow cron add "0 9 * * 1-5" "daily check" -d` | durable 标记 |
| 列出任务 | `patchflow cron list` | 显示所有任务，含状态 |
| 手动执行 | `patchflow cron run` | 执行所有到期任务 |
| 删除任务 | `patchflow cron remove <task-id>` | 删除成功 |
| 无效 cron | `patchflow cron add "invalid" "test"` | 报错 "Invalid cron" |

### 6. Worktree 隔离

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| Git repo | 在 git 仓库中运行下方代码片段 B | worktree 创建/隔离/清理成功 |
| 非 Git | 在非 git 目录运行下方代码片段 B | 使用 sandbox 回退方案 |

**代码片段 B — Worktree 验收:**

```python
from patchflow.core.worktree import WorktreeManager

wm = WorktreeManager(".")
print(f"Is git repo: {wm.is_git_repo}")

wt = wm.create("verify-test")
assert wt is not None, "Failed to create worktree"
print(f"Created: {wt.info.path}")

# 应用补丁
import os
test_file = os.path.join(wt.path, "_pf_test.txt")
with open(test_file, "w") as f:
    f.write("original")

ok = wt.apply_patches([{
    "file": "_pf_test.txt",
    "old": "original",
    "new": "modified by patchflow",
}])
assert ok, "Patch apply failed"
with open(test_file) as f:
    assert "modified by patchflow" in f.read(), "Content not updated"

# 清理
wt.discard()
print("Worktree: PASSED")
```

### 7. Browser 验证

| 测试项 | 命令 | 预期结果 |
|--------|------|----------|
| HTTP 验证 | `python -c "from patchflow.core.browser import browser_verify; r = browser_verify('https://httpbin.org/get'); print(r.summary())"` | 显示页面加载状态 |
| 无效 URL | `python -c "from patchflow.core.browser import browser_verify; r = browser_verify('https://invalid-xyz-12345.com'); print(r.summary())"` | 显示 FAILED |
| Console 检查 | `python -c "from patchflow.core.browser import ConsoleMonitor; m = ConsoleMonitor(); e = m.check('https://httpbin.org/get', timeout_ms=10000); print(f'Errors: {len(e)}')"` | 错误计数 |

### 8. User Prompt

**代码片段 C — User Prompt 验收:**

```python
from patchflow.utils.user_prompt import Question, _parse_indices

# 解析测试
assert _parse_indices("1,2,3", 5) == [1, 2, 3]
assert _parse_indices("1-3", 5) == [1, 2, 3]
assert _parse_indices("abc", 5) == [1]  # fallback

q = Question(key="lang", text="Choose", options=["Python", "Go"], default="Python")
assert q.key == "lang"
assert len(q.options) == 2
print("UserPrompt: PASSED")
```

---

## 三、核心流程集成验收

### 3.1 Build 命令集成 Web 搜索
```bash
patchflow build "写一个计算斐波那契数的函数" --research
# 预期: 日志中出现 "搜索相关文档..." 和 "找到 N 条参考结果"

patchflow build "简单 hello world" --no-research
# 预期: 不出现搜索日志
```

### 3.2 Fix 命令集成
```bash
patchflow fix "修复 app.py 中的问题"
# 预期: 日志中出现 "[AgentOrch] Web 上下文注入"
```

### 3.3 全量单元测试
```bash
pytest tests/ -v
# 预期: 453+ passed, < 10 skipped
```

---

## 四、边界情况与异常处理

| 测试场景 | 操作 | 预期行为 |
|----------|------|----------|
| 无网络 | 断网后 `patchflow search "test"` | 优雅降级，返回空结果 |
| 超大项目 Task | TaskManager 添加 100+ 任务 | 正常工作（无上限） |
| Cron 表达式错误 | `patchflow cron add "abc" "test"` | 错误提示 |
| Memory 删除不存在 | `patchflow memory forget nonexistent` | 错误提示 |
| Worktree 重复创建 | 创建同名 worktree 两次 | 自动添加后缀区分 |
| 并发 Worktree | 10 个 worktree 同时创建 | 不冲突 |

---