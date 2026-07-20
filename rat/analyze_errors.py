#!/usr/bin/env python3
"""
自动分析output目录下所有repo的analysis_result.txt
使用LLM提取错误信息，生成汇总表格和改进建议
"""

import os
import json
from pathlib import Path
from typing import List, Dict, Any
from collections import defaultdict

from libkit.llm import LLMChat


def find_analysis_files(output_dir: str = "output") -> List[Dict[str, str]]:
    """查找所有analysis_result.txt文件"""
    analysis_files = []
    output_path = Path(output_dir)

    for analysis_file in output_path.rglob("analysis_result.txt"):
        # 提取repo信息: output/owner/repo/analysis_result.txt
        parts = analysis_file.parts
        if len(parts) >= 3:
            owner = parts[-3]
            repo = parts[-2]
            analysis_files.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "full_name": f"{owner}/{repo}",
                    "path": str(analysis_file),
                }
            )

    return sorted(analysis_files, key=lambda x: x["full_name"])


def extract_errors_with_llm(analysis_content: str, llm: LLMChat) -> Dict[str, Any]:
    """使用LLM提取agent的错误行为"""

    prompt = f"""请分析以下配置过程的分析结果，重点关注 **配置代理（Agent）本身犯的错误**，而不是环境问题。

{analysis_content}

请识别Agent的错误行为，以JSON格式返回：
{{
    "success": true/false,  // 是否成功完成所有阶段
    "agent_mistakes": [  // Agent犯的错误（最多5个最严重的）
        {{
            "category": "错误类别",  // 如：引入语法错误、错误的代码修改、使用不当工具、重复无效操作、应该放弃但继续尝试、错误的依赖安装策略等
            "description": "具体描述Agent做错了什么",  // 详细说明Agent的错误行为
            "impact": "影响",  // 这个错误导致了什么后果
            "should_do": "应该怎么做"  // Agent应该采取什么正确的做法
        }}
    ],
    "stuck_stage": "卡住的阶段",  // 如：依赖安装、测试收集、测试执行等
    "agent_behavior_summary": "Agent行为总结"  // 一句话总结Agent的整体表现和主要问题
}}

重点关注：
1. Agent是否修改代码时引入了语法错误、逻辑错误
2. Agent是否使用了不当的工具（如用sed修改代码、heredoc语法错误）
3. Agent是否重复执行相同的失败操作
4. Agent是否应该更早放弃但继续尝试
5. Agent的修复策略是否合理
6. Agent是否理解了错误的根本原因

只返回JSON，不要其他内容。"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response, usage = llm.chat(messages, temperature=0.0, max_tokens=2048)
        if response:
            # 尝试解析JSON
            # 移除可能的markdown代码块标记
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            return json.loads(response)
        else:
            return {"error": "LLM返回为空"}
    except Exception as e:
        return {"error": f"解析失败: {str(e)}"}


def analyze_all_repos(model: str = "deepseek-chat") -> Dict[str, Any]:
    """分析所有repos的agent错误行为"""

    print("正在查找analysis文件...")
    analysis_files = find_analysis_files()
    print(f"找到 {len(analysis_files)} 个repos的分析文件\n")

    print("初始化LLM...")
    llm = LLMChat(model=model)

    results = []
    mistake_categories = defaultdict(list)

    for i, file_info in enumerate(analysis_files, 1):
        print(f"[{i}/{len(analysis_files)}] 分析 {file_info['full_name']}...")

        try:
            with open(file_info["path"], "r", encoding="utf-8") as f:
                content = f.read()

            # 使用LLM提取agent错误行为
            error_info = extract_errors_with_llm(content, llm)

            result = {
                "repo": file_info["full_name"],
                "owner": file_info["owner"],
                "repo_name": file_info["repo"],
                **error_info,
            }
            results.append(result)

            # 统计agent错误类型
            if "agent_mistakes" in error_info:
                for mistake in error_info["agent_mistakes"]:
                    category = mistake.get("category", "未知")
                    mistake_categories[category].append(
                        {
                            "repo": file_info["full_name"],
                            "description": mistake.get("description", ""),
                            "should_do": mistake.get("should_do", ""),
                        }
                    )

            print(f"  ✓ 成功: {error_info.get('success', 'unknown')}")
            if error_info.get("agent_behavior_summary"):
                print(f"  Agent行为: {error_info['agent_behavior_summary']}")
            if error_info.get("agent_mistakes"):
                print(f"  Agent错误数: {len(error_info['agent_mistakes'])}")
            print()

        except Exception as e:
            print(f"  ✗ 错误: {str(e)}\n")
            results.append({"repo": file_info["full_name"], "error": str(e)})

    return {
        "results": results,
        "mistake_categories": dict(mistake_categories),
        "total_repos": len(analysis_files),
        "success_count": sum(1 for r in results if r.get("success") == True),
        "failure_count": sum(1 for r in results if r.get("success") == False),
    }


def generate_markdown_report(
    analysis_data: Dict[str, Any], output_file: str = "agent_mistakes_report.md"
):
    """生成Markdown格式的Agent错误分析报告"""

    results = analysis_data["results"]
    mistake_categories = analysis_data["mistake_categories"]

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# RAT Agent错误行为分析报告\n\n")
        f.write("> 本报告重点分析配置Agent在执行过程中犯的错误，而非环境本身的问题\n\n")

        # 统计概览
        f.write("## 📊 统计概览\n\n")
        f.write(f"- 总repo数: {analysis_data['total_repos']}\n")
        f.write(f"- 成功数: {analysis_data['success_count']}\n")
        f.write(f"- 失败数: {analysis_data['failure_count']}\n")
        f.write(
            f"- 成功率: {analysis_data['success_count'] / analysis_data['total_repos'] * 100:.1f}%\n\n"
        )

        # 统计agent错误总数
        total_mistakes = sum(len(r.get("agent_mistakes", [])) for r in results)
        f.write(f"- **Agent错误总数**: {total_mistakes}\n")
        f.write(
            f"- **平均每个repo的Agent错误数**: {total_mistakes / len(results):.1f}\n\n"
        )

        # 详细表格
        f.write("## 📋 详细Agent错误表格\n\n")
        f.write("| Repo | 成功 | Agent错误数 | 卡住阶段 | Agent行为总结 |\n")
        f.write("|------|------|-------------|----------|---------------|\n")

        for result in results:
            repo = result.get("repo", "unknown")
            success = "✓" if result.get("success") else "✗"
            stuck_stage = result.get("stuck_stage", "-") or "-"
            behavior_summary = result.get("agent_behavior_summary", "-") or "-"
            mistake_count = len(result.get("agent_mistakes", []))

            f.write(
                f"| {repo} | {success} | {mistake_count} | {stuck_stage} | {behavior_summary} |\n"
            )

        f.write("\n")

        # Agent错误分类统计
        f.write("## 🔍 Agent错误类型统计\n\n")
        sorted_categories = sorted(
            mistake_categories.items(), key=lambda x: len(x[1]), reverse=True
        )

        for category, mistakes in sorted_categories:
            f.write(f"### {category} ({len(mistakes)}次)\n\n")
            for mistake_info in mistakes:
                f.write(f"**{mistake_info['repo']}**\n")
                f.write(f"- 错误: {mistake_info['description']}\n")
                f.write(f"- 应该: {mistake_info['should_do']}\n\n")

        # 详细错误案例
        f.write("## 📝 详细错误案例\n\n")
        for result in results:
            if result.get("agent_mistakes"):
                f.write(f"### {result['repo']}\n\n")
                f.write(f"**成功**: {'是' if result.get('success') else '否'}\n\n")
                f.write(
                    f"**Agent行为总结**: {result.get('agent_behavior_summary', 'N/A')}\n\n"
                )
                f.write(f"**Agent犯的错误**:\n\n")
                for i, mistake in enumerate(result["agent_mistakes"], 1):
                    f.write(f"{i}. **{mistake.get('category', '未知')}**\n")
                    f.write(f"   - 错误行为: {mistake.get('description', 'N/A')}\n")
                    f.write(f"   - 影响: {mistake.get('impact', 'N/A')}\n")
                    f.write(f"   - 应该做: {mistake.get('should_do', 'N/A')}\n\n")

        # 改进建议
        f.write("## 💡 Agent改进建议\n\n")
        f.write(generate_improvement_suggestions(analysis_data))

    print(f"\n报告已生成: {output_file}")


def generate_improvement_suggestions(analysis_data: Dict[str, Any]) -> str:
    """基于agent错误行为生成改进建议"""

    mistake_categories = analysis_data["mistake_categories"]
    results = analysis_data["results"]

    suggestions = []

    # 统计各类agent错误
    code_modification_errors = sum(
        1
        for r in results
        if any(
            "语法错误" in m.get("category", "")
            or "代码修改" in m.get("category", "")
            or "引入" in m.get("category", "")
            for m in r.get("agent_mistakes", [])
        )
    )

    tool_misuse = sum(
        1
        for r in results
        if any(
            "工具" in m.get("category", "")
            or "sed" in m.get("description", "")
            or "heredoc" in m.get("description", "")
            for m in r.get("agent_mistakes", [])
        )
    )

    repeated_failures = sum(
        1
        for r in results
        if any(
            "重复" in m.get("category", "") or "重复" in m.get("description", "")
            for m in r.get("agent_mistakes", [])
        )
    )

    should_give_up = sum(
        1
        for r in results
        if any(
            "放弃" in m.get("category", "") or "应该放弃" in m.get("should_do", "")
            for m in r.get("agent_mistakes", [])
        )
    )

    suggestions.append("### 🎯 核心问题总结\n")
    suggestions.append(
        f"通过分析 {len(results)} 个repos，发现Agent主要存在以下问题：\n"
    )
    suggestions.append(f"- 代码修改引入错误: {code_modification_errors} 个repos")
    suggestions.append(f"- 工具使用不当: {tool_misuse} 个repos")
    suggestions.append(f"- 重复无效操作: {repeated_failures} 个repos")
    suggestions.append(f"- 应该放弃但继续尝试: {should_give_up} 个repos\n")

    suggestions.append("### 1️⃣ 代码修改安全性\n")
    if code_modification_errors > 0:
        suggestions.append(
            f"**问题严重程度**: 🔴 高 ({code_modification_errors}/{len(results)} repos受影响)\n"
        )
        suggestions.append("**典型错误**:")
        suggestions.append("- 修改代码时引入语法错误（未闭合的字符串、括号不匹配）")
        suggestions.append("- 使用sed/awk等文本工具修改Python代码导致破坏")
        suggestions.append("- 修改类型提示时破坏了代码结构\n")
        suggestions.append("**改进措施**:")
        suggestions.append(
            "- ✅ **强制验证**: 任何代码修改后立即使用 `ast.parse()` 验证语法"
        )
        suggestions.append("- ✅ **禁用危险工具**: 禁止使用 sed/awk 修改 Python 代码")
        suggestions.append(
            "- ✅ **使用AST工具**: 使用 libcst、rope 等AST-based重构工具"
        )
        suggestions.append("- ✅ **备份机制**: 修改前备份文件，失败时自动回滚")
        suggestions.append(
            "- ✅ **类型提示处理**: 使用 `from __future__ import annotations` 而不是直接修改代码\n"
        )
    else:
        suggestions.append("**状态**: ✅ 良好\n")

    suggestions.append("### 2️⃣ 工具选择和使用\n")
    if tool_misuse > 0:
        suggestions.append(
            f"**问题严重程度**: 🟡 中 ({tool_misuse}/{len(results)} repos受影响)\n"
        )
        suggestions.append("**典型错误**:")
        suggestions.append("- 使用 heredoc 语法时超时或格式错误")
        suggestions.append("- pip install 命令中特殊字符（<、>、换行符）未正确转义")
        suggestions.append("- 使用 cat/echo 创建复杂文件导致语法错误\n")
        suggestions.append("**改进措施**:")
        suggestions.append(
            "- ✅ **使用专用工具**: 用 edit-file 而不是 sed，用 Write 而不是 cat/echo"
        )
        suggestions.append(
            "- ✅ **命令验证**: pip install 前验证参数格式，特殊字符加引号"
        )
        suggestions.append(
            "- ✅ **避免heredoc**: 对于复杂内容，使用 Write 工具而不是 heredoc"
        )
        suggestions.append("- ✅ **Shell安全**: 所有shell命令参数都应该正确转义\n")
    else:
        suggestions.append("**状态**: ✅ 良好\n")

    suggestions.append("### 3️⃣ 错误恢复策略\n")
    if repeated_failures > 0 or should_give_up > 0:
        suggestions.append(
            f"**问题严重程度**: 🟡 中 ({max(repeated_failures, should_give_up)}/{len(results)} repos受影响)\n"
        )
        suggestions.append("**典型错误**:")
        suggestions.append("- 重复执行相同的失败命令，期望不同结果")
        suggestions.append("- 遇到不可恢复的错误（如包不存在）仍继续尝试")
        suggestions.append("- 没有记录失败原因，导致循环错误\n")
        suggestions.append("**改进措施**:")
        suggestions.append("- ✅ **失败记录**: 记录每次失败的命令和原因，避免重复")
        suggestions.append(
            "- ✅ **早期放弃**: 识别不可恢复错误（包不存在、Python版本不兼容、缺少系统权限）"
        )
        suggestions.append("- ✅ **最大重试次数**: 同一操作失败3次后自动放弃")
        suggestions.append("- ✅ **根因分析**: 失败后分析根本原因，而不是盲目重试")
        suggestions.append(
            "- ✅ **降级策略**: 如果无法修复，尝试跳过相关测试而不是卡住\n"
        )
    else:
        suggestions.append("**状态**: ✅ 良好\n")

    suggestions.append("### 4️⃣ 依赖安装策略\n")
    suggestions.append("**改进措施**:")
    suggestions.append("- ✅ **分阶段安装**: 系统依赖 → 构建工具 → Python包")
    suggestions.append("- ✅ **版本检测**: 安装前检查Python版本兼容性")
    suggestions.append("- ✅ **网络容错**: 自动切换镜像源（apt、pip），添加重试机制")
    suggestions.append(
        "- ✅ **预检查**: 对于复杂依赖（dlib、opencv），先检查构建依赖是否满足\n"
    )

    suggestions.append("### 5️⃣ 测试执行策略\n")
    suggestions.append("**改进措施**:")
    suggestions.append("- ✅ **环境检测**: 测试前检测Docker、Redis等基础设施可用性")
    suggestions.append(
        "- ✅ **超时处理**: 检测需要下载大文件的测试，自动跳过或增加超时"
    )
    suggestions.append("- ✅ **Mock策略**: 对外部API依赖提供mock")
    suggestions.append("- ✅ **分离测试**: 区分环境配置测试和功能测试\n")

    suggestions.append("### 6️⃣ 优先级改进建议\n")
    suggestions.append("**立即实施（P0）**:")
    suggestions.append("1. 代码修改后强制语法验证")
    suggestions.append("2. 禁用sed/awk修改Python代码")
    suggestions.append("3. 记录失败操作，避免重复\n")
    suggestions.append("**短期实施（P1）**:")
    suggestions.append("1. 实现自动备份和回滚机制")
    suggestions.append("2. 添加不可恢复错误识别")
    suggestions.append("3. 改进工具选择逻辑\n")
    suggestions.append("**长期优化（P2）**:")
    suggestions.append("1. 构建知识库，记录常见问题解决方案")
    suggestions.append("2. 实现智能重试策略")
    suggestions.append("3. 添加性能监控和优化")

    return "\n".join(suggestions)


def main():
    """主函数"""
    print("=" * 60)
    print("RAT Agent错误行为分析工具")
    print("=" * 60)
    print()

    # 分析所有repos的agent错误行为
    analysis_data = analyze_all_repos(model="deepseek-chat")

    # 生成报告
    generate_markdown_report(analysis_data)

    # 同时保存JSON格式
    json_file = "agent_mistakes_data.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(analysis_data, f, ensure_ascii=False, indent=2)
    print(f"JSON数据已保存: {json_file}")

    print("\n" + "=" * 60)
    print("分析完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
