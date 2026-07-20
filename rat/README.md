# RunAnyThing (RAT)

## 📖 Introduction

**RunAnyThing (RAT)** is an LLM-powered automated build system designed to analyze GitHub repositories and automatically construct executable environments within Docker containers. The system utilizes **Intelligent Agents** to autonomously handle dependency installation, environment configuration, and build testing.

## 🌐 Streamlit Web Interface

The project now includes a Streamlit-based web interface (`simple_app.py`) named **RunAnyThing**, offering:

* **Intuitive Form Interface:** Easily input all `env_main.py` parameters.
* **Real-time Streaming Output:** Monitor the build process as it happens.
* **One-Click Execution:** Run tasks and view results instantly.
* **User-Friendly Interaction:** No need to memorize complex command-line arguments.

Using the Streamlit frontend is the recommended way to interact with the system for a smoother experience.

## 🔧 Prerequisites

* **Python:** 3.10+
* **Docker:** Must be running
* **Git**
* **LLM API Keys:** DeepSeek, OpenAI, GLM-4, or Qwen (DashScope)
* **GitHub Personal Access Token:** Required for repository access

## 📦 Installation

1. **Clone the repository:**

    ```bash
    <Hidden Repository Address>
    ```

2. **Install dependencies (via pip or uv):**

    Using **pip** (Standard):
    ```bash
    pip install -e .
    ```

    Using **[uv](https://github.com/astral-sh/uv)** (Recommended - Faster):
    ```bash
    uv pip install -e .
    ```

3. **Configure Environment Variables (Crucial):**

    Copy the template and fill in your credentials:
    ```bash
    cp .env.example .env
    ```

    Edit the `.env` file and `libkit/tools/llm.py` with your API keys:

    ```bash
    # LLM API Keys (At least one required)
    DEEPSEEK_API_KEY=your_deepseek_api_key_here
    GLM_API_KEY=your_glm_api_key_here
    OPENAI_API_KEY=your_openai_api_key_here
    TONGYI_API_KEY=your_tongyi_api_key_here

    # GitHub Token (Required)
    GITHUB_TOKEN=your_github_token_here

    # Optional
    WANDB_API_KEY=your_wandb_api_key_here
    ```

4. **Run the Streamlit Interface:**

    ```bash
    streamlit run simple_app.py
    ```

    Open the displayed address (usually `http://localhost:8501`) in your browser.

## 🚀 Quick Start

### CLI Basic Usage:

```bash
python env_main.py --full_name "owner/repo-name" --root_path . --llm "deepseek-chat"

```

#### Argument Reference

* `--full_name`: GitHub repository (Format: `owner/repo`).
* `--root_path`: Working directory path (Default: current directory).
* `--llm`: LLM model to use (Default: `deepseek-chat`).
* `--num_turn`: Maximum iterations for the Agent (Default: `10`).
* `--hitl`: Enable **Human-In-The-Loop** mode (Optional).
* `--save_mode`: Options:
* `none`: Do not save (Default).
* `dockerfile`: Save the generated Dockerfile.
* `image`: Save as a local Docker image.



#### Examples

```bash
# Basic run
python env_main.py --full_name "streamlit/streamlit-example" --root_path . --llm "deepseek-chat"

# Save as Dockerfile
python env_main.py --full_name "owner/repo" --root_path . --save_mode dockerfile

# Save as Docker Image
python env_main.py --full_name "owner/repo" --root_path . --save_mode image

# Enable HITL (Human-In-The-Loop)
python env_main.py --full_name "owner/repo" --root_path . --hitl
```

### Dataset Building

```bash
# Fetch Python repos containing Dockerfiles and unit tests
python build_dataset/fetch_python_docker_repos.py --target-count 100

# Use Docker Agent workflow to build images for fetched repos
python build_dataset/run_docker_agent_workflow.py --limit 1 --workers 1
```

## ✨ Key Features

### 1. Intelligent Environment Analysis

* Automatically analyzes repository structures.
* Recommends optimal Python versions and Docker base images.
* Identifies dependency managers (pip, poetry, conda, etc.).

### 2. Automated Configuration

* Installs dependencies automatically within Docker.
* Resolves dependency conflicts autonomously.
* Configures the runtime environment.

### 3. Build Verification

* Automatically discovers and executes tests.
* Validates that the environment is correctly configured.

### 4. Flexible Export Options

* **none**: Ephemeral environment for quick testing.
* **dockerfile**: Generates a reusable `Dockerfile`.
* **image**: Commits the state to a local Docker image.

### 5. Toolset Support

The system provides several specialized tools for the Agent:

* `construct-test`: Auto-creates test cases.
* `read-file`: Deep analysis of source files.
* `search-repo`: Codebase searching.
* `view-outline`: Visualizes code hierarchy.
* `retrieve-image`: Recommends Docker images.

See [TOOL_COMMAND_USAGE.md](https://www.google.com/search?q=TOOL_COMMAND_USAGE.md) for details.

## 📂 Project Structure

```text
RunAnyThing/
├── env_main.py             # Main entry point
├── libkit/                 # Core Library
│   ├── environment.py      # Docker environment management
│   ├── setupagent.py       # Analysis Agent
│   ├── codeagent.py        # Configuration Agent
│   ├── tool.py             # Tool definitions
│   └── ...
├── build_agent/            # Build agents (Legacy)
├── input/                  # Storage for cloned repositories
├── output/                 # Storage for build results
└── requirements.txt        # Python dependencies
```

## 🔄 Workflow

1. **Clone**: Downloads the repo from GitHub.
2. **Analyze**: `SetupAgent` scans the code and recommends a base image.
3. **Create**: A Docker container is initialized based on recommendations.
4. **Configure**: `CodeAgent` performs terminal operations to set up the environment.
5. **Export**: Results are saved according to the selected `save_mode`.

## 📝 Output

Upon completion, results are stored in `output/<owner>/<repo>/`:

* `trajectory.json`: Full log of Agent actions and reasoning.
* `Dockerfile`: Generated if `save_mode` is `dockerfile`.
* `IMAGE_USAGE.md`: Instructions if `save_mode` is `image`.

## 🔍 FAQ

**Q: Which LLM should I choose?**
A: We support `deepseek-chat`, `gpt-4o`, and more. `deepseek-chat` is highly recommended for its excellent balance of performance and cost.

**Q: Does it clean up Docker containers?**
A: Yes, containers are automatically stopped and removed after the process finishes unless you use `--save_mode`.

**Q: Which languages are supported?**
A: Currently, we focus on Python projects. Support for more languages is on our roadmap.

## 📋 Roadmap

### Core Features

* [x] Token consumption tracking per tool call.
* [x] Support for more languages (Node.js, Java, Go).
* [x] Optimization of Agent logic to reduce error rates.
* [x] Success rate statistics and analysis.
* [x] RAG system for issue retrieval (`retrieve_issue`).

### UX & Performance

* [x] Web UI (Streamlit).
* [x] Real-time configuration progress display.
* [ ] Environment rollback support.
* [x] Parallel processing for multiple repositories.
* [x] Caching for common dependencies and images.

## 📄 License

Apache License 2.0

## 📮 Contact

If you have questions or suggestions, feel free to open an **Issue** or submit a **Pull Request**.

