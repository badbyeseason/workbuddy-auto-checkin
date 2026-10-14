#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到技能 —— 一键安装器（跨平台，仅用标准库）

作用：把本目录（技能包）安装到 WorkBuddy 的技能目录，并自动自检。
      使用者不需要知道"技能目录在哪"。

用法
----
  python install.py                 # 安装到默认技能目录并自检
  python install.py --dry-run       # 只显示将要做什么，不动任何文件
  python install.py --dest <DIR>    # 安装到自定义技能根目录
  python install.py --no-doctor     # 安装后跳过自检
  python install.py --uninstall     # 卸载（移除已安装的技能）

默认技能目录
------------
  Windows : %USERPROFILE%\\.workbuddy\\skills
  macOS   : ~/.workbuddy/skills
  Linux   : ~/.workbuddy/skills
  可用环境变量 WORKBUDDY_SKILLS_DIR 覆盖。

退出码：0 成功；1 失败。
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_NAME = "workbuddy-daily-checkin"

# 需要安装进技能目录的条目（相对技能包根目录）
PAYLOAD = ["SKILL.md", "安装与分享.md", "install.py", "README.md", "scripts"]
SKIP_DIRS = {".git", "__pycache__", "dist", ".idea", ".vscode"}


def skills_root() -> Path:
    env = os.environ.get("WORKBUDDY_SKILLS_DIR")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~")) / ".workbuddy" / "skills"


def current_python() -> str:
    return sys.executable or ("python" if os.name == "nt" else "python3")


def human(p: Path) -> str:
    return str(p)


def copy_payload(src_root: Path, dst: Path) -> int:
    """复制技能文件，返回复制文件数。"""
    count = 0
    for name in PAYLOAD:
        s = src_root / name
        if not s.exists():
            continue
        if s.is_dir():
            for p in s.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in SKIP_DIRS for part in p.parts):
                    continue
                rel = p.relative_to(src_root)
                d = dst / rel
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, d)
                count += 1
        else:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst / name)
            count += 1
    return count


def run_doctor(target: Path) -> int:
    script = target / "scripts" / "checkin.py"
    if not script.is_file():
        print("  ! 未找到 scripts/checkin.py，跳过自检")
        return 1
    print("\n—— 安装后自检（不联网、不写缓存）——")
    try:
        p = subprocess.run([current_python(), str(script), "--doctor"],
                           capture_output=True, timeout=120)
    except Exception as e:
        print(f"  ! 自检执行失败：{type(e).__name__}: {e}")
        return 1
    out = p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else str(p.stdout)
    print(out.rstrip())
    if p.returncode != 0:
        print("  ⚠ 自检提示本机为受限环境（macOS 属预期，见 SKILL.md 平台支持矩阵）")
    return 0 if p.returncode == 0 else 0  # 自检不阻断安装


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 WorkBuddy 每日签到技能")
    ap.add_argument("--dest", default=None,
                    help="技能根目录（默认 ~/.workbuddy/skills）")
    ap.add_argument("--dry-run", action="store_true", help="只显示计划，不写文件")
    ap.add_argument("--no-doctor", action="store_true", help="安装后跳过自检")
    ap.add_argument("--uninstall", action="store_true", help="卸载该技能")
    args = ap.parse_args()

    src_root = Path(__file__).resolve().parent
    dst_root = Path(args.dest).expanduser() if args.dest else skills_root()
    target = dst_root / SKILL_NAME
    py = current_python()

    print("=" * 66)
    print(" WorkBuddy 每日签到技能 · 一键安装")
    print("=" * 66)
    print(f"技能包来源 : {human(src_root)}")
    print(f"技能目录根 : {human(dst_root)}")
    print(f"安装目标   : {human(target)}")
    print(f"Python     : {py}")

    if not (src_root / "SKILL.md").is_file():
        print("\n✗ 当前目录看起来不是技能包（缺少 SKILL.md）。")
        print("  请在解压后的 workbuddy-daily-checkin 目录内运行本脚本。")
        return 1

    if args.uninstall:
        if not target.exists():
            print("\n该技能未安装，无需卸载。")
            return 0
        if args.dry_run:
            print(f"\n[dry-run] 将删除：{human(target)}")
            return 0
        shutil.rmtree(target)
        print(f"\n✓ 已卸载：{human(target)}")
        print("  提示：定时任务请到 WorkBuddy 的自动化列表中另行删除。")
        return 0

    # 备份同名旧技能
    backup = None
    if target.exists():
        backup = target.with_name(f"{SKILL_NAME}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        if args.dry_run:
            print(f"\n[dry-run] 已存在同名技能，将先备份到：{human(backup)}")
            print(f"[dry-run] 随后复制 {len(PAYLOAD)} 类文件到：{human(target)}")
            return 0
        try:
            shutil.move(str(target), str(backup))
            print(f"\n· 已存在同名技能，先备份为：{human(backup)}")
        except Exception as e:
            print(f"\n✗ 备份失败，已中止以免覆盖：{type(e).__name__}: {e}")
            return 1
    elif args.dry_run:
        print(f"\n[dry-run] 将复制 {len(PAYLOAD)} 类文件到：{human(target)}")
        return 0

    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        n = copy_payload(src_root, target)
    except Exception as e:
        print(f"\n✗ 复制失败：{type(e).__name__}: {e}")
        if backup and backup.exists() and not target.exists():
            shutil.move(str(backup), str(target))
            print("  已回滚到安装前状态。")
        return 1

    print(f"\n✓ 已安装 {n} 个文件到：{human(target)}")

    if not args.no_doctor:
        run_doctor(target)

    print("\n—— 下一步（只需在 WorkBuddy 对话里说一句）——")
    print("  帮我设置每天 9:30 自动运行 workbuddy-daily-checkin 技能，并把签到结果简要报告给我。")
    print("\n提示：签到的前提是 WorkBuddy 保持运行；软件没开时会自动跳过，不算失败。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
