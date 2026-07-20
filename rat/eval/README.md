# Evaluation Scripts

This directory contains evaluation scripts for different repository analysis approaches.

## Structure

```
eval/
├── common/              # Shared utilities and base classes
│   ├── base_model.py    # BaseEvalModel abstract class
│   ├── scorers.py       # All scorer functions (pytest, npm, cargo, java, etc.)
│   ├── utils.py         # Shared utilities (TimeoutException, load_repos, etc.)
│   └── eval_runner.py   # Common evaluation workflow
│
├── models/              # Model implementations
│   ├── rat_model.py     # RATModel (SetupAgentV2 + CodeAgent)
│   ├── sweagent_model.py # SWEAgentModel (SWE-agent baseline)
│   ├── pipreqs_model.py # PipreqsModel (pipreqs baseline)
│   ├── repo2run_model.py # Repo2RunModel (Repo2Run baseline)
│   ├── installamatic_model.py # InstallamaticModel (Installamatic baseline)
│   └── zeroshot_model.py # ZeroShotModel (direct LLM Dockerfile generation)
│
├── ours/                # Our evaluation approaches
│   └── eval_rat.py      # RAT evaluation script (our method)
│
├── sweagent/            # SWE-agent evaluation
│   └── eval_sweagent.py # SWE-agent evaluation script
│
├── pipreqs/             # Pipreqs evaluation
│   └── eval_pipreqs.py  # Pipreqs evaluation script
│
├── repo2run/            # Repo2Run evaluation
│   ├── eval_repo2run.py # Repo2Run evaluation script
│   ├── test_repo2run_single.py # Single repo test
│   └── README.md        # Repo2Run documentation
│
├── installamatic/       # Installamatic evaluation
│   └── eval_installamatic.py # Installamatic evaluation script
│
└── zeroshot/            # ZeroShot evaluation
    ├── eval_zeroshot.py # ZeroShot evaluation script
    └── README.md        # ZeroShot documentation
```

## Usage

### RAT Model (Our Method)
```bash
python eval/ours/eval_rat.py \
  --repos-json datasets/all_repos.json \
  --root-path . \
  --llm deepseek-chat \
  --num-turn 15 \
  --timeout 900 \
  --language python
```

### SWE-agent Model
```bash
python eval/sweagent/eval_sweagent.py \
  --repos-json datasets/all_repos.json \
  --root-path . \
  --llm deepseek-chat \
  --num-turn 15 \
  --timeout 900 \
  --swe-agent-cost-limit 2.0 \
  --language python
```

### Pipreqs Model
```bash
python eval/pipreqs/eval_pipreqs.py \
  --repos-json datasets/python/python_repos_all.json \
  --root-path . \
  --timeout 900 \
  --language python
```

### Repo2Run Model
```bash
python eval/repo2run/eval_repo2run.py \
  --root-path . \
  --llm deepseek-chat \
  --num-turn 100 \
  --timeout 7200 \
  --language python
```

### Installamatic Model
```bash
python eval/installamatic/eval_installamatic.py \
  --root-path . \
  --llm deepseek-chat \
  --num-turn 3 \
  --timeout 1200 \
  --language python
```

### ZeroShot Model
```bash
python eval/zeroshot/eval_zeroshot.py \
  --root-path . \
  --llm deepseek-chat \
  --max-context-tokens 8000 \
  --timeout 600 \
  --language python
```

## Key Components

### Common Scorers
All evaluation scripts share the same scorers for fair comparison:
- `success_scorer` - Basic success/failure
- `pytest_pass_rate_scorer` - Python test pass rate
- `pytest_collect_scorer` - Python test collection success
- `npm_install_scorer` - Node.js dependency installation
- `npm_test_pass_rate_scorer` - Node.js test pass rate  
- `cargo_build_scorer` - Rust build success
- `cargo_test_pass_rate_scorer` - Rust test pass rate
- `java_build_scorer` - Java Maven/Gradle build success

### BaseEvalModel
All models inherit from `BaseEvalModel` which provides:
- `_check_timeout()` - Timeout checking
- `predict()` - Abstract method for processing repositories

### Evaluation Runner
`run_evaluation()` encapsulates the common workflow:
1. Initialize Weave/WandB
2. Load dataset
3. Select appropriate scorers
4. Create and run evaluation
5. Handle cleanup

## Adding a New Model

1. Create a new model class in `eval/models/`:
```python
from eval.common.base_model import BaseEvalModel
import weave

class MyModel(BaseEvalModel):
    # Add model-specific attributes
    my_param: str
    
    @weave.op
    def predict(self, repo: dict) -> dict:
        # Implement your logic here
        return {
            "status": "success",
            "root_path": self.root_path,
            "full_name": repo["full_name"],
        }
```

2. Create an entry point script:
```python
from eval.common.eval_runner import run_evaluation
from eval.models.my_model import MyModel

def main():
    args = parse_args()
    return run_evaluation(
        args=args,
        model_class=MyModel,
        model_kwargs={"root_path": args.root_path, ...},
        weave_project="my-evaluation",
        language=args.language,
    )
```

## Migration Notes

### Changes from Old Structure
- `eval_all_repos.py` → `eval/ours/eval_rat.py`
- `eval_utils.py` → `eval/common/utils.py`
- Models extracted from scripts into `eval/models/`
- Scorers consolidated into `eval/common/scorers.py`
- Common workflow extracted to `eval/common/eval_runner.py`

### Benefits
- **DRY**: No code duplication for scorers and utilities
- **Maintainable**: Changes to common logic in one place
- **Extensible**: Easy to add new models
- **Consistent**: All models use the same scorers for fair comparison
