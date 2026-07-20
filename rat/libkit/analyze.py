import json
import os
from libkit.llm import LLMChat


def analyze_config_issues(full_name, root_path, full_analysis=False):
    """Analyze issues during environment setup/configuration.

    Args:
        full_name: Repository full name (e.g., user/repo)
        root_path: Root path
        full_analysis: If True, show full detailed analysis; otherwise only key errors

    Returns:
        str: Analysis result; None if failed
    """
    output_dir = (
        f"{root_path}/output/{full_name.split('/')[0]}/{full_name.split('/')[1]}"
    )

    # Read JSON files
    def read_json(filepath):
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Failed to read {filepath}: {e}")
        return None

    inner_commands = read_json(f"{output_dir}/inner_commands.json")
    outer_commands = read_json(f"{output_dir}/outer_commands.json")
    trajectory = read_json(f"{output_dir}/trajectory.json")

    if not trajectory:
        return None

    # Prepare analysis prompt
    if full_analysis:
        prompt = f"""
Please analyze issues during the following setup/configuration process. You are given the contents of three files:
1. inner_commands.json: command execution sequence including return code, time, and command.
2. outer_commands.json: tool command execution including LLM_time and command.
3. trajectory.json: the full conversation history of the setup agent.

Please analyze:
1. Which commands failed (non-zero return code)? What are the likely causes?
2. Based on trajectory.json, did the setup agent complete all stages successfully? If not, where did it get stuck?
3. What is the most likely failure point in the overall setup process?

Please provide a detailed analysis report.

=== inner_commands.json (first 50 records, total {len(inner_commands) if inner_commands else 0}) ===
{json.dumps(inner_commands[:50] if inner_commands else [], indent=2, ensure_ascii=False)}

=== outer_commands.json (first 50 records, total {len(outer_commands) if outer_commands else 0}) ===
{json.dumps(outer_commands[:50] if outer_commands else [], indent=2, ensure_ascii=False)}

=== trajectory.json (full content) ===
{json.dumps(trajectory, indent=2, ensure_ascii=False)}
"""
        system_content = "You are a senior software engineer and system setup expert, skilled at analyzing setup issues and errors."
        max_tokens = 2000
    else:
        prompt = f"""
Please briefly analyze issues during the following setup/configuration process, focusing on the error location and key problems:

1. inner_commands.json: command execution sequence including return code, time, and command.
2. outer_commands.json: tool command execution including LLM_time and command.
3. trajectory.json: the full conversation history of the setup agent.

Please focus on:
1. Which commands failed (non-zero return code)? Briefly explain why.
2. Did the setup agent complete all stages successfully? If not, where did it get stuck?

Answer in concise bullet points. Do not write a long report. Explain what went wrong, and do not use **.

=== inner_commands.json (first 50 records, total {len(inner_commands) if inner_commands else 0}) ===
{json.dumps(inner_commands[:50] if inner_commands else [], indent=2, ensure_ascii=False)}

=== outer_commands.json (first 50 records, total {len(outer_commands) if outer_commands else 0}) ===
{json.dumps(outer_commands[:50] if outer_commands else [], indent=2, ensure_ascii=False)}

=== trajectory.json (full content) ===
{json.dumps(trajectory, indent=2, ensure_ascii=False)}
"""
        system_content = "You are a senior software engineer and system setup expert, skilled at quickly identifying key setup errors. Answer in concise bullet points. Do not use **."
        max_tokens = 1000

    # Call LLM
    llm = LLMChat(model="deepseek-chat")
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt},
    ]

    response, usage = llm.chat(messages, temperature=0.0, max_tokens=max_tokens)
    return response
