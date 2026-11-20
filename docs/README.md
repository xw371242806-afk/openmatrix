# OpenMatrix 文档索引

> 用途：快速定位 OpenMatrix 仓库的代码结构、架构设计、MCP 工具清单与工程规范。

---

## 一、仓库代码结构（顶层）

| 目录 / 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 主服务：`/api/analyze`、`/api/v1/analyze-chain`、`/api/v1/analyze-batch`、引文与支持度校验、导出、REST API |
| `mcp_server.py` | MCP 服务：52 个工具（检索 / 引文 / 财务 KPI / 数据源 / 导出 / 任务 / 调度） |
| `agent/` | 编排层：规则规划器 / 可选 LLM 规划器 / `research_run` 状态机 / 工具白名单 / 持久化调度器 |
| `core/` | 适配层：模型路由与客户端生命周期、答案支持度校验、Map-Reduce 调度、访问鉴权 |
| `storage.py` | `MetadataStore` / `ObjectStore`（默认 SQLite + 本地磁盘，可选 PostgreSQL + S3/MinIO） |
| `library.py` | 项目文档库后端（支持 PDF / DOCX / TXT 上传与物理页码保留） |
| `apikeys.py` / `audit.py` / `backup.py` | API Key 管理 / 审计日志 / 备份恢复 |
| `model_router.py` | 按任务类型选择模型（fast / chain / batch） |
| `tests/` | Python 测试套件（`python -m pytest -q`） |
| `ui/` | React 19 + Vite + TypeScript Research Desk 前端（`Overview` / `Projects` / `Analysis` / `Results` / `Agent` / `Trace` / `Quality` / `Evidence` / `Settings`） |
| `scripts/` | 运维、发布打包与 E2E 验证脚本（`make_release.py`、`verify_running_service.py`、`ui_smoke_test.py` 等） |
| `browser-extension/` | 网页证据快照浏览器扩展（MV3） |
| `docs/` | 工程与架构文档（见下表） |

---

## 二、文档导航

| 文件 | 内容 |
|---|---|
| `README.md` / `README.zh-CN.md` | 项目总览、快速开始、部署与验证命令、免责声明 |
| `CHANGELOG.md` | 版本发布记录（`v0.1.0`） |
| `ROADMAP.md` | 产品演进路线图（已交付与 `v0.2.0` 规划） |
| `PATENT_COMPLIANCE.md` | 知识产权与技术合规护栏（无网格工作流建议与红线） |
| `SECURITY.md` | 漏洞私有上报路径与内置安全加固说明 |
| `THIRD_PARTY_NOTICES.md` | 后端与前端第三方开源依赖与许可证台账 |
| `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` | 贡献规范、DCO 签署与行为准则 |
| `MCP_INTEGRATION.md` | OpenMatrix × MCP 集成架构说明 |
| `docs/MCP-ROADMAP.md` | 52 个 MCP 工具分组清单与架构边界 |
| `docs/MCP-CLIENTS.md` | 外部 MCP 客户端接入指南 |
| `docs/COMPONENT-CATALOG.md` | 已评估的生产组件目录与准入门槛 |
| `docs/CAPABILITY-REGISTRY.md` | 候选组件评分与许可证政策记录 |
| `docs/WORKFLOW.md` | 工程状态机、测试门禁与评审规范 |
| `docs/MATRIX-DEEP-DIVE-SETUP.md` | Matrix Deep Dive 可选扩展配置说明 |
| `docs/RELEASE-CHECKLIST.md` | Windows 干净环境发布包手工验收清单 |
