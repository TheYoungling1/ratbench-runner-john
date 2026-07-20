#!/usr/bin/env python3
"""
批量运行 env_main.py 脚本，从文件中读取仓库列表。
用法示例:
    python env_main_batch.py --input test/python_repo_name.txt --root_path . --save_mode none
    python env_main_batch.py --input test/python_repo_name.txt --root_path . --save_mode none --parallel 2
"""

import argparse
import subprocess
import sys
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple


def read_repo_names(input_file: str) -> List[str]:
    """
    从输入文件中读取仓库名称，跳过空行和注释（以#开头的行）。
    每行格式应为: owner/repo
    """
    repo_names = []
    if not os.path.exists(input_file):
        print(f"错误: 输入文件不存在: {input_file}")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 移除可能的尾部注释
            if "#" in line:
                line = line.split("#")[0].strip()
            if line:
                repo_names.append(line)

    print(f"从 {input_file} 读取了 {len(repo_names)} 个仓库名称")
    sys.stdout.flush()
    return repo_names


def run_env_main(
    full_name: str,
    root_path: str,
    save_mode: str,
    llm: str = "deepseek-chat",
    num_turn: int = 25,
    hitl: bool = False,
    use_dockerfile: bool = False,
    custom_plan: bool = False,
    realtime_output: bool = False,
) -> Tuple[bool, str]:
    """
    运行单个 env_main.py 进程。
    返回: (成功状态, 输出消息)
    """
    # 构建命令
    cmd = [
        sys.executable,
        "env_main.py",
        "--full_name",
        full_name,
        "--root_path",
        root_path,
        "--save_mode",
        save_mode,
        "--llm",
        llm,
        "--num_turn",
        str(num_turn),
    ]
    if hitl:
        cmd.append("--hitl")
    if use_dockerfile:
        cmd.append("--use-dockerfile")
    if custom_plan:
        cmd.append("--custom-plan")

    print(f"开始处理仓库: {full_name}")
    print(f"执行命令: {' '.join(cmd)}")

    start_time = time.time()
    try:
        # 运行子进程
        if realtime_output:
            # 实时输出模式：直接显示输出
            print(f"实时输出模式启用，开始处理仓库: {full_name}")
            sys.stdout.flush()
            result = subprocess.run(
                cmd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=sys.stdout,
                stderr=sys.stderr,
                timeout=7200,  # 2小时超时，与 env_main.py 一致
            )
            # 实时模式下，输出直接显示到控制台
        else:
            # 捕获输出模式（默认）
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,  # 2小时超时，与 env_main.py 一致
            )
        end_time = time.time()
        elapsed = end_time - start_time

        if result.returncode == 0:
            print(f"✅ 成功处理仓库: {full_name} (耗时: {elapsed:.2f}秒)")
            sys.stdout.flush()
            return True, f"成功: {full_name}"
        else:
            print(f"❌ 处理仓库失败: {full_name} (退出码: {result.returncode})")
            sys.stdout.flush()
            if not realtime_output and result.stderr:
                print(f"标准错误输出:\n{result.stderr[:1000]}")  # 只打印前1000字符
            elif realtime_output:
                print("（错误输出已实时显示）")
            return False, f"失败: {full_name} (退出码: {result.returncode})"

    except subprocess.TimeoutExpired:
        print(f"⏰ 处理仓库超时: {full_name} (超过2小时)")
        return False, f"超时: {full_name}"
    except Exception as e:
        print(f"⚠️  处理仓库异常: {full_name} - {str(e)}")
        return False, f"异常: {full_name} - {str(e)}"


def main():
    parser = argparse.ArgumentParser(
        description="批量运行 env_main.py 脚本处理多个仓库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 顺序处理所有仓库
  python env_main_batch.py --input test/python_repo_name.txt --root_path . --save_mode none

  # 并行处理，最多2个并发
  python env_main_batch.py --input test/python_repo_name.txt --root_path . --save_mode none --parallel 2

  # 使用自定义参数
  python env_main_batch.py --input test/python_repo_name.txt --root_path . --save_mode dockerfile --llm gpt-4 --hitl
        """,
    )

    parser.add_argument(
        "--input",
        type=str,
        default="test/python_repo_name.txt",
        help="包含仓库列表的输入文件路径 (默认: test/python_repo_name.txt)",
    )
    parser.add_argument(
        "--root_path",
        type=str,
        required=True,
        help="根路径，与 env_main.py 的 --root_path 参数相同",
    )
    parser.add_argument(
        "--save_mode",
        type=str,
        default="none",
        choices=["none", "dockerfile", "image"],
        help="保存模式: none-不保存, dockerfile-保存Dockerfile和文件, image-保存为本地镜像",
    )
    parser.add_argument(
        "--llm",
        type=str,
        default="deepseek-chat",
        help="使用的LLM模型名称 (默认: deepseek-chat)",
    )
    parser.add_argument(
        "--num_turn", type=int, default=25, help="每个仓库的最大交互轮数 (默认: 25)"
    )
    parser.add_argument(
        "--hitl", action="store_true", help="是否使用人机交互模式 (HITL)"
    )
    parser.add_argument(
        "--use-dockerfile",
        action="store_true",
        help="使用仓库自带的 Dockerfile（如果存在）",
    )
    parser.add_argument("--custom-plan", action="store_true", help="使用自定义计划模式")
    parser.add_argument(
        "--realtime-output",
        action="store_true",
        help="实时显示子进程输出（默认：捕获输出）",
    )
    parser.add_argument(
        "--parallel", type=int, default=1, help="并行处理的数量 (默认: 1，即顺序处理)"
    )
    parser.add_argument("--skip", type=int, default=0, help="跳过前 N 个仓库 (默认: 0)")
    parser.add_argument(
        "--max_items", type=int, default=0, help="最大处理数量 (0表示处理所有，默认: 0)"
    )

    args = parser.parse_args()

    print(f"开始批量处理... (输入文件: {args.input})")
    sys.stdout.flush()

    # 读取仓库列表
    repo_names = read_repo_names(args.input)

    if not repo_names:
        print("错误: 没有找到有效的仓库名称")
        sys.exit(1)

    # 应用跳过和最大数量限制
    if args.skip > 0:
        if args.skip >= len(repo_names):
            print(
                f"警告: 跳过数量 {args.skip} 大于仓库总数 {len(repo_names)}，没有仓库需要处理"
            )
            sys.exit(0)
        repo_names = repo_names[args.skip :]
        print(f"跳过了前 {args.skip} 个仓库，剩余 {len(repo_names)} 个")

    if args.max_items > 0 and args.max_items < len(repo_names):
        repo_names = repo_names[: args.max_items]
        print(f"限制处理前 {args.max_items} 个仓库")

    print(f"开始处理 {len(repo_names)} 个仓库...")
    print(f"根路径: {args.root_path}")
    print(f"保存模式: {args.save_mode}")
    print(f"LLM: {args.llm}")
    print(f"并行度: {args.parallel}")
    print(f"实时输出: {'是' if args.realtime_output else '否'}")
    print("-" * 50)

    # 准备参数
    common_kwargs = {
        "root_path": args.root_path,
        "save_mode": args.save_mode,
        "llm": args.llm,
        "num_turn": args.num_turn,
        "hitl": args.hitl,
        "use_dockerfile": args.use_dockerfile,
        "custom_plan": args.custom_plan,
        "realtime_output": args.realtime_output,
    }

    results = []
    start_total = time.time()

    if args.parallel <= 1:
        # 顺序处理
        for i, full_name in enumerate(repo_names, 1):
            print(f"\n[{i}/{len(repo_names)}] ", end="")
            success, message = run_env_main(full_name, **common_kwargs)
            results.append((success, message))
    else:
        # 并行处理
        print(f"使用 {args.parallel} 个线程并行处理...")
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            # 提交所有任务
            future_to_repo = {
                executor.submit(run_env_main, full_name, **common_kwargs): full_name
                for full_name in repo_names
            }

            # 收集结果
            for i, future in enumerate(as_completed(future_to_repo), 1):
                full_name = future_to_repo[future]
                try:
                    success, message = future.result()
                    results.append((success, message))
                    print(f"[{i}/{len(repo_names)}] 完成: {full_name}")
                except Exception as e:
                    print(f"[{i}/{len(repo_names)}] 任务异常: {full_name} - {str(e)}")
                    results.append((False, f"异常: {full_name} - {str(e)}"))

    end_total = time.time()
    total_elapsed = end_total - start_total

    # 统计结果
    success_count = sum(1 for success, _ in results if success)
    failure_count = len(results) - success_count

    print("\n" + "=" * 50)
    print("批量处理完成!")
    print(f"总计处理: {len(results)} 个仓库")
    print(f"成功: {success_count} 个")
    print(f"失败: {failure_count} 个")
    print(f"总耗时: {total_elapsed:.2f} 秒")

    if failure_count > 0:
        print("\n失败的仓库:")
        for success, message in results:
            if not success:
                print(f"  - {message}")

    # 保存结果到文件
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    result_file = f"batch_result_{timestamp}.txt"
    with open(result_file, "w", encoding="utf-8") as f:
        f.write(f"批量处理结果 - {timestamp}\n")
        f.write(f"输入文件: {args.input}\n")
        f.write(f"根路径: {args.root_path}\n")
        f.write(f"保存模式: {args.save_mode}\n")
        f.write(f"LLM: {args.llm}\n")
        f.write(f"并行度: {args.parallel}\n")
        f.write(f"总计处理: {len(results)} 个仓库\n")
        f.write(f"成功: {success_count} 个\n")
        f.write(f"失败: {failure_count} 个\n")
        f.write(f"总耗时: {total_elapsed:.2f} 秒\n\n")

        f.write("详细结果:\n")
        for i, (success, message) in enumerate(results, 1):
            status = "✅ 成功" if success else "❌ 失败"
            f.write(f"{i}. {status}: {message}\n")

    print(f"\n详细结果已保存到: {result_file}")

    # 如果有失败，返回非零退出码
    if failure_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
