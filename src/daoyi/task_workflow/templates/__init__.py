"""Built-in workflow templates for common task patterns.

These are pre-defined workflows that ship with OpenHarness.
They can be overridden by user-saved workflows in ~/.daoyi/workflows/.
"""

from daoyi.task_workflow.models import TaskPhase, TaskWorkflow

# ── 1. Code review / audit ──────────────────────────────────────

code_review = TaskWorkflow(
    id="code_review",
    trigger_patterns=[
        "review", "code review", "audit", "check.*code",
        "review.*code", "检查.*代码", "审查.*代码",
    ],
    description="Review code for issues, bugs, and improvements",
    phases=[
        TaskPhase(
            name="understand",
            prompt_template=(
                "你正在执行「理解代码」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 read_file 工具读取相关文件\n"
                "2. 调用 glob 搜索文件路径\n"
                "3. 调用 grep 搜索代码中的关键模式\n\n"
                "规则：必须通过调用工具来获取信息。不要只输出猜测的内容。"
            ),
            tools=["read_file", "glob", "grep", "bash"],
            max_turns=3,
        ),
        TaskPhase(
            name="analyze",
            prompt_template=(
                "你正在执行「分析代码」阶段。\n"
                "用户需求：{user_input}\n"
                "之前阶段完成：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 grep 搜索潜在问题模式（安全漏洞、性能问题等）\n"
                "2. 调用 bash 运行静态分析工具（如 pylint、eslint）\n"
                "3. 针对发现的问题，调用 read_file 深入阅读相关代码\n\n"
                "规则：先调用工具验证你的判断，再输出分析结论。"
            ),
            tools=["grep", "bash", "read_file"],
            max_turns=3,
        ),
        TaskPhase(
            name="report",
            prompt_template=(
                "你正在执行「生成报告」阶段。\n"
                "用户需求：{user_input}\n"
                "分析结果：\n{phase_results}\n\n"
                "你的任务：\n"
                 "调用 write_file 工具将审查报告写入文件 review-结果.md\n"
                 "报告应包含：文件路径、行号、问题严重程度、修改建议。\n\n"
                 "规则：必须调用 write_file 工具写出文件，不要只输出文字。"
            ),
            tools=["write_file", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── 2. Write code / implement feature ───────────────────────────

write_code = TaskWorkflow(
    id="write_code",
    trigger_patterns=[
        "write.*script", "write.*code", "implement", "create.*function",
        "编写", "实现", "写一段", "写个", "写一个.*脚本", "写一个.*程序",
        "用 write_file", "创建.*脚本", "创建.*程序",
    ],
    description="Write code or scripts to solve a task",
    phases=[
        TaskPhase(
            name="understand",
            prompt_template=(
                "你正在执行「理解需求」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 bash 确认当前目录结构（ls）\n"
                "2. 如果用户提到现有文件，调用 read 读取它\n"
                "3. 调用 glob/grep 搜索相关代码\n\n"
                "规则：必须通过调用工具来了解环境。确认清楚后再进入下一阶段。"
            ),
            tools=["read_file", "glob", "grep", "bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="implement",
            prompt_template=(
                "你正在执行「编写代码」阶段。\n"
                "用户需求：{user_input}\n"
                "环境信息：{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 write_file 工具创建代码文件（必须写入真实文件，不要只输出文本）\n"
                "2. 如果修改现有文件，调用 edit_file 工具\n"
                "3. 调用 bash 检查语法（python -c \"compile(...)\" 或 node --check）\n\n"
                "重要——你**必须**调用 write_file 工具来实际创建文件。\n"
                "只输出代码文字而不调用 write_file 工具是**错误的**。\n"
                "先调用 write_file，不要提前输出解释文字。"
            ),
            tools=["write_file", "edit_file", "read_file", "bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="verify",
            prompt_template=(
                "你正在执行「验证」阶段。\n"
                "用户需求：{user_input}\n"
                "已创建的文件见上方「已创建的文件」。\n"
                "之前的输出：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 read_file 确认文件是否存在\n"
                "2. 调用 bash 运行生成的代码或脚本（如 python 文件名.py）\n"
                "3. 验证输出是否符合预期\n"
                "4. 如果文件不存在或运行失败，调用 write_file 重新创建\n\n"
                "规则：必须调用 bash 实际执行代码来验证。如果找不到文件，用查找工具先定位。"
            ),
            tools=["bash", "read_file", "write_file"],
            max_turns=2,
        ),
    ],
)

# ── 3. File operations (read/search) ────────────────────────────

file_search = TaskWorkflow(
    id="file_search",
    trigger_patterns=[
        "find.*file", "search.*file", "look for", "where is",
        "查找", "查找.*文件", "找.*文件",
        "^搜索", "^查询 ", "^搜一下", "^查一下",
    ],
    description="Search and read files in the codebase",
    phases=[
        TaskPhase(
            name="search",
            prompt_template=(
                "你正在执行「搜索文件」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 glob 搜索文件名模式\n"
                "2. 调用 grep 搜索文件内容中的关键字\n"
                "3. 调用 bash 使用 find 命令辅助搜索\n\n"
                "规则：必须调用工具来搜索，不要猜测文件位置。"
            ),
            tools=["glob", "grep", "bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="read_file",
            prompt_template=(
                "你正在执行「读取文件」阶段。\n"
                "用户需求：{user_input}\n"
                "搜索结果：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 read_file 工具读取找到的文件\n"
                "2. 调用 bash 获取文件信息（大小、行数等）\n"
                "3. 以清晰的格式展示文件内容摘要\n\n"
                "规则：先调用 read_file 工具，再输出内容摘要。"
            ),
            tools=["read_file", "bash"],
            max_turns=2,
        ),
    ],
)

# ── 4. Debug / fix issues ───────────────────────────────────────

debug_fix = TaskWorkflow(
    id="debug_fix",
    trigger_patterns=[
        "debug", "fix.*bug", "error", "not working", "crash",
        "调试", "修复.*bug", "报错", "出错",
    ],
    description="Debug and fix issues in code",
    phases=[
        TaskPhase(
            name="investigate",
            prompt_template=(
                "你正在执行「调查问题」阶段。\n"
                "用户描述的问题：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 bash 运行出错的命令，复现问题\n"
                "2. 调用 read 读取相关源代码\n"
                "3. 调用 grep 搜索错误信息的线索\n\n"
                "规则：必须通过工具复现问题，不要仅凭经验猜测。"
            ),
            tools=["read_file", "grep", "glob", "bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="fix",
            prompt_template=(
                "你正在执行「修复问题」阶段。\n"
                "用户需求：{user_input}\n"
                "调查结果：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 edit_file 工具修改有问题的代码\n"
                "2. 调用 bash 验证修改结果\n"
                "3. 日志文件中可能有关键线索，调用 read 读取\n\n"
                "规则：必须调用 edit_file 工具来修复代码，只输出建议是不完整的。"
            ),
            tools=["edit_file", "read_file", "write_file", "bash"],
            max_turns=3,
        ),
        TaskPhase(
            name="verify",
            prompt_template=(
                "你正在执行「验证修复」阶段。\n"
                "用户需求：{user_input}\n"
                "修复内容：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 bash 重新运行之前出错的命令\n"
                "2. 如果缺少文件，调用 read_file 检查\n"
                "3. 确认问题已修复，没有引入新问题\n"
                "4. 总结修复方案\n\n"
                "规则：必须调用 bash 实际运行来验证。遇到文件缺失先用 read_file 定位。"
            ),
            tools=["bash", "read_file", "write_file"],
            max_turns=2,
        ),
    ],
)

# ── 5. Bash / shell operations ──────────────────────────────────

bash_ops = TaskWorkflow(
    id="bash_ops",
    trigger_patterns=[
        "^run ", "^bash ", "^ls ", "^pwd ", "^cd ", "^echo ", "^运行 ", "^运行",
        "^cat ", "^grep ", "^find ", "^chmod ", "^mkdir ",
        "bash.*script", "shell.*command",
        r"\./", "sh ",
        "^打开", "^启动", "^关闭", "创建.*脚本",
    ],
    description="Execute shell commands and scripts",
    phases=[
        TaskPhase(
            name="execute",
            prompt_template=(
                "严格按照以下步骤执行。不允许跳过任何步骤。\n"
                "用户需求：{user_input}\n\n"
                "步骤1 — 如果命令涉及打开/启动应用程序，先运行 ls /Applications/ 查看准确的 App 名称（如 Google Chrome.app 而非 Chrome）\n"
                "步骤2 — 再用正确的名称执行用户要求的命令\n"
                "步骤3 — 展示并解释输出结果"
            ),
            tools=["bash", "read_file"],
            max_turns=3,
        ),
    ],
)

# ── 6. Refactor / modify existing code ──────────────────────────

refactor_code = TaskWorkflow(
    id="refactor_code",
    trigger_patterns=[
        "refactor", "重构", "modify.*code", "改.*代码",
        "update.*function", "optimize",
    ],
    description="Refactor or modify existing code",
    phases=[
        TaskPhase(
            name="understand",
            prompt_template=(
                "你正在执行「理解代码」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 read 读取需要修改的文件\n"
                "2. 调用 grep 搜索相关函数和引用\n"
                "3. 调用 glob 找到相关文件\n\n"
                "规则：先理解完整代码，再决定如何修改。"
            ),
            tools=["read_file", "grep", "glob", "bash"],
            max_turns=3,
        ),
        TaskPhase(
            name="modify",
            prompt_template=(
                "你正在执行「修改代码」阶段。\n"
                "用户需求：{user_input}\n"
                "原始代码：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 edit_file 工具修改目标文件\n"
                "2. 调用 read 验证修改结果\n"
                "3. 调用 bash 运行语法检查\n\n"
                "规则：必须调用 edit_file 工具做修改。只输出建议文本是无效的。"
            ),
            tools=["edit_file", "read_file", "bash", "write_file"],
            max_turns=2,
        ),
        TaskPhase(
            name="verify",
            prompt_template=(
                "你正在执行「验证」阶段。\n"
                "用户需求：{user_input}\n"
                "修改内容：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 bash 运行测试或执行相关命令\n"
                "2. 如果文件缺失，调用 read_file 检查\n"
                "3. 确认功能正常\n"
                "4. 总结修改了哪些内容\n\n"
                "规则：必须调用 bash 实际验证。"
            ),
            tools=["bash", "read_file", "write_file"],
            max_turns=2,
        ),
    ],
)

# ── 7. Git operations ───────────────────────────────────────────

git_ops = TaskWorkflow(
    id="git_ops",
    trigger_patterns=[
        "git", "commit", "push", "pull", "merge", "branch",
        "提交代码", "推送", "拉取",
        "^git ", "status",
    ],
    description="Execute Git operations (status, add, commit, push, pull, etc.)",
    phases=[
        TaskPhase(
            name="check_status",
            prompt_template=(
                "你正在执行「检查仓库状态」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行 git status 查看仓库状态\n"
                "2. 调用 bash 执行 git log --oneline -5 查看最近提交\n"
                "3. 确认当前分支和仓库状态\n\n"
                "规则：先通过 bash 工具了解仓库状态。"
            ),
            tools=["bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="execute",
            prompt_template=(
                "你正在执行「执行 Git 操作」阶段。\n"
                "用户需求：{user_input}\n"
                "仓库状态：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行用户要求的 git 命令\n"
                "2. 输出执行结果\n\n"
                "规则：必须调用 bash 工具来执行 git 命令。"
            ),
            tools=["bash"],
            max_turns=2,
        ),
    ],
)

# ── 8. Run tests ────────────────────────────────────────────────

run_tests = TaskWorkflow(
    id="run_tests",
    trigger_patterns=[
        "run.*test", "test.*suite", "pytest", "unittest", "run spec",
        "运行.*测试", "跑.*测试", "执行.*测试",
        "npm test", "go test", "cargo test",
    ],
    description="Discover and run test suites",
    phases=[
        TaskPhase(
            name="discover",
            prompt_template=(
                "你正在执行「发现测试」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 glob 搜索测试文件（*test*.py, *_test.go, *.spec.ts, __tests__/ 等）\n"
                "2. 调用 bash 检查项目使用的测试框架（package.json 中的 test script）\n"
                "3. 如果有配置文件（pytest.ini, jest.config.js），调用 read_file 读取\n\n"
                "规则：先了解项目的测试结构。"
            ),
            tools=["glob", "grep", "bash", "read_file"],
            max_turns=2,
        ),
        TaskPhase(
            name="run",
            prompt_template=(
                "你正在执行「运行测试」阶段。\n"
                "用户需求：{user_input}\n"
                "测试配置：\n{phase_results}\n\n"
                "你的任务：\n"
                "1. 调用 bash 运行测试命令（如 pytest, npm test, go test ./...）\n"
                "2. 输出测试结果摘要\n"
                "3. 如果有失败的测试，调用 read_file/grep 读取相关源码分析原因\n\n"
                "规则：必须调用 bash 实际运行测试。"
            ),
            tools=["bash", "read_file", "grep"],
            max_turns=2,
        ),
    ],
)

# ── 9. Install dependencies ─────────────────────────────────────

install_deps = TaskWorkflow(
    id="install_deps",
    trigger_patterns=[
        "install", "npm install", "pip install", "bundle install",
        "安装", "安装.*依赖", "安装.*包", "安装.*模块",
        "go mod", "cargo build", "yarn add",
    ],
    description="Install project dependencies",
    phases=[
        TaskPhase(
            name="detect",
            prompt_template=(
                "你正在执行「检测依赖」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 glob 查找依赖配置文件（package.json, requirements.txt, Cargo.toml, go.mod, Gemfile 等）\n"
                "2. 调用 read_file 读取依赖文件了解需要安装的内容\n\n"
                "规则：先确认项目类型和依赖管理器。"
            ),
            tools=["glob", "read_file", "bash"],
            max_turns=2,
        ),
        TaskPhase(
            name="install",
            prompt_template=(
                "你正在执行「安装依赖」阶段。\n"
                "用户需求：{user_input}\n"
                "项目信息：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行对应的安装命令\n"
                "   - Node.js: npm install / yarn / pnpm install\n"
                "   - Python: pip install -r requirements.txt 或 pip install <包名>\n"
                "   - Rust: cargo build\n"
                "   - Go: go mod download\n"
                "2. 确认安装成功，如果有错误尝试修复\n\n"
                "规则：必须调用 bash 执行安装命令。"
            ),
            tools=["bash", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── 10. Docker operations ───────────────────────────────────────

docker_ops = TaskWorkflow(
    id="docker_ops",
    trigger_patterns=[
        "docker", "docker-compose", "docker compose", "container",
        "构建.*镜像", "运行.*容器", "docker.*部署",
    ],
    description="Build, run, and manage Docker containers",
    phases=[
        TaskPhase(
            name="check",
            prompt_template=(
                "你正在执行「检查 Docker 环境」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 bash 检查 docker 是否安装（docker --version）\n"
                "2. 调用 bash 检查 Dockerfile 或 docker-compose.yml 是否存在\n"
                "3. 调用 glob 搜索 docker 相关配置文件\n\n"
                "规则：先确认环境和配置。"
            ),
            tools=["bash", "glob", "read_file"],
            max_turns=2,
        ),
        TaskPhase(
            name="execute",
            prompt_template=(
                "你正在执行「执行 Docker 操作」阶段。\n"
                "用户需求：{user_input}\n"
                "环境信息：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行用户要求的 docker 命令\n"
                "   - 构建: docker build -t <name> .\n"
                "   - 运行: docker run <image>\n"
                "   - Compose: docker compose up -d\n"
                "2. 输出执行结果\n\n"
                "规则：必须调用 bash 来执行 docker 命令。"
            ),
            tools=["bash", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── 11. Database / migration operations ─────────────────────────

db_ops = TaskWorkflow(
    id="db_ops",
    trigger_patterns=[
        "database", "migration", "migrate", "db.*操作",
        "数据库", "迁移.*数据库",
        "prisma", "sequelize", "typeorm", "django.*migrate",
        "alembic", "flyway",
    ],
    description="Run database migrations and queries",
    phases=[
        TaskPhase(
            name="detect",
            prompt_template=(
                "你正在执行「检测数据库配置」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 glob 搜索数据库配置文件（prisma/schema.prisma, ormconfig.json, .env, alembic.ini 等）\n"
                "2. 调用 read_file 读取配置文件了解数据库类型和迁移工具\n"
                "3. 调用 bash 检查当前数据库迁移状态\n\n"
                "规则：先了解项目的数据库配置。"
            ),
            tools=["glob", "read_file", "bash"],
            max_turns=3,
        ),
        TaskPhase(
            name="execute",
            prompt_template=(
                "你正在执行「执行数据库操作」阶段。\n"
                "用户需求：{user_input}\n"
                "数据库配置：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行用户要求的数据库操作\n"
                "   - Prisma: npx prisma migrate dev / npx prisma db push\n"
                "   - Alembic: alembic upgrade head\n"
                "   - Django: python manage.py migrate\n"
                "   - TypeORM: npx typeorm migration:run\n"
                "2. 输出执行结果，如果有错误尝试分析\n\n"
                "规则：必须调用 bash 执行数据库操作。"
            ),
            tools=["bash", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── 12. Web search / research ───────────────────────────────────

web_research = TaskWorkflow(
    id="web_research",
    trigger_patterns=[
        "search.*web", "research", "查找.*资料", "搜索.*信息",
        "查一下", "google", "baidu",
        "how to", "what is", "find.*information",
        "搜索.*网站", "查询.*资料", "搜.*信息",
        "^搜索", "^查询 ", "^搜一下", "^查一下",
    ],
    description="Search the web and gather information",
    phases=[
        TaskPhase(
            name="search",
            prompt_template=(
                "你正在执行「搜索信息」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 web_search 或 web_fetch 搜索相关信息\n"
                "2. 如果有多个关键词，分批次搜索\n\n"
                "规则：调用搜索工具获取信息后再整理结果。"
            ),
            tools=["web_search", "web_fetch"],
            max_turns=3,
        ),
        TaskPhase(
            name="summarize",
            prompt_template=(
                "你正在执行「整理结果」阶段。\n"
                "用户需求：{user_input}\n"
                "搜索结果：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 如果信息量大，调用 write_file 工具将结果写入文件\n"
                "2. 以清晰的格式呈现关键信息，包含来源链接\n\n"
                "规则：先调用工具搜索和保存，再输出整理后的结果。"
            ),
            tools=["write_file"],
            max_turns=2,
        ),
    ],
)

# ── 13. File format conversion / data processing ────────────────

data_process = TaskWorkflow(
    id="data_process",
    trigger_patterns=[
        "convert", "format.*convert", "parse.*file",
        "转换.*格式", "解析.*文件", "处理.*数据",
        "csv.*json", "json.*csv", "xml.*json", "yaml",
        "提取.*数据", "处理.*日志",
    ],
    description="Convert file formats and process data",
    phases=[
        TaskPhase(
            name="understand",
            prompt_template=(
                "你正在执行「理解数据」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 read_file 读取源文件了解数据格式\n"
                "2. 调用 bash 查看文件大小和前几行内容\n\n"
                "规则：先了解源数据的格式和结构。"
            ),
            tools=["read_file", "bash", "glob"],
            max_turns=2,
        ),
        TaskPhase(
            name="process",
            prompt_template=(
                "你正在执行「处理数据」阶段。\n"
                "用户需求：{user_input}\n"
                "源数据信息：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 write_file 创建处理脚本（Python 脚本用于数据转换）\n"
                "2. 调用 bash 运行脚本\n"
                "3. 调用 read_file 验证输出文件内容\n\n"
                "规则：必须先写脚本再运行，不要手动处理大量数据。"
            ),
            tools=["write_file", "bash", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── 14. Server/service management ───────────────────────────────

server_mgmt = TaskWorkflow(
    id="server_mgmt",
    trigger_patterns=[
        "start.*server", "stop.*server", "restart.*service",
        "启动.*服务", "停止.*服务", "重启.*服务",
        "pm2", "supervisor", "systemctl",
        "部署.*服务", "查看.*日志",
    ],
    description="Manage servers, services, and processes",
    phases=[
        TaskPhase(
            name="check",
            prompt_template=(
                "你正在执行「检查服务状态」阶段。\n"
                "用户需求：{user_input}\n\n"
                "你的任务：\n"
                "1. 调用 bash 检查服务状态（如 pm2 status, systemctl status, ps aux）\n"
                "2. 调用 bash 检查端口占用（lsof -i, netstat）\n"
                "3. 如果有配置文件，调用 read_file 读取\n\n"
                "规则：先确认当前服务状态。"
            ),
            tools=["bash", "read_file", "glob"],
            max_turns=2,
        ),
        TaskPhase(
            name="operate",
            prompt_template=(
                "你正在执行「操作服务」阶段。\n"
                "用户需求：{user_input}\n"
                "服务状态：\n{accumulated_context}\n\n"
                "你的任务：\n"
                "1. 调用 bash 执行用户要求的操作（启动/停止/重启）\n"
                "2. 调用 bash 确认操作结果\n"
                "3. 如果需要修改配置，调用 edit_file 修改配置文件\n\n"
                "规则：必须调用 bash 执行操作命令。"
            ),
            tools=["bash", "edit_file", "read_file"],
            max_turns=2,
        ),
    ],
)

# ── All built-in templates ──────────────────────────────────────

BUILTIN_WORKFLOWS: list[TaskWorkflow] = [
    code_review,
    write_code,
    file_search,
    debug_fix,
    bash_ops,
    refactor_code,
    git_ops,
    run_tests,
    install_deps,
    docker_ops,
    db_ops,
    web_research,
    data_process,
    server_mgmt,
]
