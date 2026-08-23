# Repo Dockerizer Agent

这是一个基于 LLM 的 Agent，旨在自动为 GitHub 仓库配置可执行的 Docker 环境。

> **Running the RAT benchmark + scoring (EBSR/ESSR):** see [docs/RUNNING_RATBENCH.md](docs/RUNNING_RATBENCH.md).

## 核心组件

1. **Sandbox (sandbox.py)**: 使用 Docker SDK 运行指令，并具有基于 `commit` 的回滚机制。
2. **Planner (planner.py)**: 使用 ReAct (Thought/Action/Observation) 模式进行环境配置规划。
3. **Synthesizer (synthesizer.py)**: 记录成功的指令并生成最终的 `Dockerfile`。

## 安装与运行 (推荐使用 uv)

1. **创建并激活环境**:
```bash
   uv venv
   source .venv/bin/activate  # MacOS/Linux
   # .\.venv\Scripts\activate  # Windows
```
2. **安装依赖**：
```bash
uv pip install -r requirements.txt
```
3. **运行Agent**:
```bash
python agent.py <GITHUB_REPO_URL>
```

## 配置

将 `.env.example` 重命名为 `.env` 并填写你的 `MINIMAX_API_KEY`。如果使用兼容 OpenAI SDK 的网关，也可以继续使用 `OPENAI_API_KEY` / `OPENAI_API_BASE`。

## 使用

```bash
python agent.py <GITHUB_REPO_URL>
```

例如：
```bash
python agent.py https://github.com/psf/requests
```

## Multi-Docker-Eval 评估

本项目已适配 **Multi-Docker-Eval** benchmark，可用于评估自动化环境配置能力。

### 快速开始

```bash
# 1. 运行适配测试
./verify_multi_docker_eval.sh

# 2. 处理 Multi-Docker-Eval 数据集
python multi_docker_eval_adapter.py \
    path/to/Multi-Docker-Eval/task.jsonl \
    --output-dir ./eval_results \
    --model MiniMax-M2.7-highspeed
```

详细文档见 [`doc/MULTI_DOCKER_EVAL.md`](doc/MULTI_DOCKER_EVAL.md)

**相关资源**:
- [Multi-Docker-Eval 论文](https://arxiv.org/abs/2512.06915)
- [Multi-Docker-Eval GitHub](https://github.com/Z2sJ4t/Multi-Docker-Eval)
- [数据集](https://huggingface.co/datasets/litble/Multi-Docker-Eval)

## 注意事项

- 运行此 Agent 需要本地已安装并运行 Docker Engine。
- Agent 会在每一步执行前对容器进行 `commit`，这可能会占用较多磁盘空间，建议在结束后清理镜像。
