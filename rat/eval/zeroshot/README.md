# ZeroShot Evaluation

A simple baseline method that uses direct LLM-based Dockerfile generation without iterative refinement.

## Method

The ZeroShot approach follows these steps:

1. **Context Collection**: Gather repository information including:
   - README file content
   - File structure (using `tree` or `find` command)
   - Important configuration files (requirements.txt, setup.py, package.json, etc.)

2. **Context Truncation**: Limit the context to prevent LLM token overflow
   - Default: 8000 tokens (~24000 characters)
   - Proportionally distribute budget across README, file structure, and config files

3. **LLM Generation**: Send context to LLM with a prompt to generate a Dockerfile
   - Single-shot generation (no refinement loops)
   - Low temperature (0.1) for consistency

4. **Build & Test**: Build the Docker image and run tests
   - Standard pytest execution
   - Same test tools as other evaluation methods

## Usage

### Basic Usage

```bash
python eval/zeroshot/eval_zeroshot.py \
  --root-path . \
  --llm deepseek-chat \
  --timeout 600 \
  --language python
```

### With Custom Settings

```bash
python eval/zeroshot/eval_zeroshot.py \
  --root-path . \
  --llm deepseek-chat \
  --max-context-tokens 10000 \
  --timeout 900 \
  --use-eval \
  --limit 10 \
  --weave-project my-zeroshot-eval
```

## Arguments

- `--root-path`: Project root path (default: current directory)
- `--use-eval`: Use eval split instead of full dataset
- `--limit`: Maximum number of repositories to process
- `--offset`: Skip the first N repositories
- `--llm`: LLM model name (default: deepseek-chat)
- `--max-context-tokens`: Maximum tokens for repository context (default: 8000)
- `--timeout`: Per-repo timeout in seconds (default: 600)
- `--weave-project`: W&B Weave project name (default: rat-zeroshot-evaluation)
- `--language`: Repository language (default: python)

## Environment Variables

The model uses OpenAI-compatible API:

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"  # Optional

# Or use DeepSeek API
export DEEPSEEK_API_KEY="your-deepseek-key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
```

## Comparison with Other Methods

| Method | Approach | Iterations | Context Collection |
|--------|----------|------------|-------------------|
| **ZeroShot** | Direct LLM generation | Single-shot | Simple file reading |
| **Installamatic** | Gather + Repair agents | Multi-turn repair | Structured document gathering |
| **Repo2Run** | Configuration agent | Multi-turn config | Interactive exploration |
| **RAT** | Setup + Code agents | Multi-turn setup | Agentic analysis |

## Advantages

- **Simple**: Minimal complexity, easy to understand
- **Fast**: No iterative refinement loops
- **Low cost**: Single LLM call per repository
- **Baseline**: Good reference point for comparison

## Limitations

- **No error correction**: Cannot fix build failures
- **Context overflow**: May miss important information in large repos
- **No exploration**: Cannot discover hidden dependencies
- **Single attempt**: No retry mechanism

## Implementation Details

### Context Collection

The model collects repository context with truncation:

```python
# README: ~1/3 of budget
# File structure: ~1/3 of budget (max 5000 chars)
# Config files: Remaining budget (~1/3)
```

### Dockerfile Generation

Uses a system prompt that instructs the LLM to:
- Use appropriate base images
- Install all dependencies
- Set up working directory
- Enable test execution

### Build Context

Creates a separate build context directory to avoid polluting the repository:
```
output/
  {owner}/{repo}/
    Dockerfile          # Generated Dockerfile
    build_context/      # Copy of repo + Dockerfile for building
    run_pytest_results.json
    run_pytest_collect_results.json
```

## Example Output

```
================================================
[ZeroShot] Processing: owner/repo
================================================
📥 Downloading repository...
📚 Collecting repository context...
   Context collected: 15234 characters
🤖 Generating Dockerfile with LLM...
   Dockerfile generated successfully
   Dockerfile saved to: output/owner/repo/Dockerfile
🐳 Building Docker image...
✅ Build succeeded. Image: zeroshot-owner-repo
🧪 Running tests...
   Detected WORKDIR: /app
   Running pytest collect...
   Running pytest...
✅ Evaluation completed. Time: 45.2s
```

## Future Improvements

Potential enhancements for the ZeroShot method:

1. **Smart file selection**: Use heuristics to select most important files
2. **Language detection**: Automatically detect programming language
3. **Template matching**: Use Dockerfile templates for common frameworks
4. **Parallel generation**: Generate multiple Dockerfiles and pick the best
5. **Caching**: Cache generated Dockerfiles for similar repositories
