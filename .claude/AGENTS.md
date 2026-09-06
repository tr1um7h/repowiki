# Agent Rules for RepoWiki

## Progressive Rendering Wiki 更新规则

**规则：** 当前解析 wiki 采用 "progressive rendering"（渐进式渲染），每执行完一小部分任务（如某个模块解析完成），需要重新编译并更新 wiki HTML 文件。

**原因：**
- Wiki 采用渐进式渲染机制
- 用户希望实时看到生成进度
- 每个模块完成后立即更新，提供即时反馈

**实施方式：**
- `repowiki check` **默认自动更新 wiki**（progressive rendering）
- 任务成功完成后（status → done），自动调用 `run_site` 更新 HTML
- 如需禁用自动更新，使用 `--no-auto-site` 选项

**使用示例：**
```bash
# Subagent 执行任务后（默认自动更新 wiki）
repowiki check --task <task_id>

# 批量检查时（默认自动更新）
repowiki check --all

# 禁用自动更新
repowiki check --task <task_id> --no-auto-site
```

**相关文件：**
- `src/repowiki/cli.py` - CLI 参数定义（auto_site 默认 True）
- `src/repowiki/dispatch.py` - check 命令实现（包含 `_run_site_silent`）
- `src/repowiki/site.py` - 渐进式渲染实现

**当前状态：** ✅ 已默认启用自动更新