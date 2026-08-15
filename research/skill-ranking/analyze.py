#!/usr/bin/env python3
"""Classify a frozen skills.sh leaderboard snapshot and summarize it."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


OFFICE = "办公协作/结构化数据操作"
MEDIA = "内容/多媒体生成"
CLOUD = "云平台/数据平台/DevOps"
SOFTWARE = "软件工程/代码质量"
WORKFLOW = "Agent 工作流/生产力"
META = "Meta Skill/Skill 管理"
UI = "前端/UI/设计"
RESEARCH = "研究/检索/教学"
BROWSER = "浏览器/Web 自动化"
MARKETING = "营销/内容运营"


MEDIA_NAMES = re.compile(
    r"^(ai-video-generation|ai-image-generation|ai-avatar-video|"
    r"remotion-render|character-design-sheet|storyboard-creation|"
    r"product-photography|youtube-thumbnail-design|app-store-screenshots|"
    r"image-to-video|video-ad-specs|general-video|embedded-captions|"
    r"motion-graphics|faceless-explainer|slideshow|product-launch-video)$"
)

UI_NAMES = re.compile(
    r"^(frontend-design|web-design-guidelines|design-an-interface|"
    r"landing-page-design|landing-page-conversion-audit)$"
)

META_NAMES = re.compile(
    r"^(find-skills|setup-matt-pocock-skills|skill-creator|"
    r"writing-great-skills|write-a-skill|using-superpowers|agent-tools)$"
)

WORKFLOW_NAMES = re.compile(
    r"^(grill-me|grill-with-docs|grilling|handoff|prototype|triage|"
    r"to-spec|to-tickets|to-prd|to-issues|wayfinder|task-matt|"
    r"writing-for-agents|wait-what|wizard|to-questionnaire|loop-me|"
    r"claude-handoff|brainstorming|zoom-out|caveman|caveman-compress|"
    r"caveman-help|caveman-stats|writing-shape|writing-fragments|"
    r"writing-beats|orchestration)$"
)


def classify(skill: dict[str, Any]) -> str:
    source = skill["source"]
    name = skill["name"]

    if source in {"open.feishu.cn", "larksuite/cli"}:
        return META if "skill-maker" in name else OFFICE
    if source in {
        "microsoft/azure-skills",
        "prisma/skills",
        "neondatabase/agent-skills",
        "supabase/agent-skills",
    }:
        return CLOUD
    if source in {
        "prime-skills/runcomfy-agent-skills",
        "heygen-com/hyperframes",
        "remotion-dev/skills",
    } or MEDIA_NAMES.search(name):
        return MEDIA
    if source in {
        "leonxlnx/taste-skill",
        "uizze.com",
        "emilkowalski/skills",
        "nextlevelbuilder/ui-ux-pro-max-skill",
        "shadcn/ui",
        "shadcn-ui/ui",
        "pbakaus/impeccable",
    } or UI_NAMES.search(name):
        return UI
    if (
        re.search(r"agent-browser|scrapegraphai", source)
        or source
        in {
            "antibrow/anti-detect-browser-skills",
            "liarjsdev/liarjs-skills",
        }
        or re.search(r"agent-browser|web-search", name)
    ):
        return BROWSER
    if source == "autonnel/autonnel-skills" or name in {
        "twitter-automation",
        "competitor-teardown",
        "product-hunt-launch",
    }:
        return MARKETING
    if META_NAMES.search(name):
        return META
    if re.search(r"research|paper-context|teach|scaffold-exercises", name):
        return RESEARCH
    if WORKFLOW_NAMES.search(name):
        return WORKFLOW
    return SOFTWARE


def summarize(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_installs = sum(skill["installs"] for skill in skills)

    for skill in skills:
        grouped[classify(skill)].append(skill)

    summaries = []
    for category, items in grouped.items():
        source_max: dict[str, int] = {}
        for item in items:
            source_max[item["source"]] = max(
                item["installs"], source_max.get(item["source"], 0)
            )
        install_sum = sum(item["installs"] for item in items)
        summaries.append(
            {
                "category": category,
                "count": len(items),
                "install_sum": install_sum,
                "share_percent": round(install_sum / total_installs * 100, 2),
                "median": statistics.median(item["installs"] for item in items),
                "source_count": len(source_max),
                "source_normalized_sum": sum(source_max.values()),
                "top3": [
                    item["name"]
                    for item in sorted(
                        items, key=lambda item: item["installs"], reverse=True
                    )[:3]
                ],
            }
        )
    return sorted(summaries, key=lambda item: item["install_sum"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--csv",
        type=Path,
        help="Optionally write every ranked Skill and its primary category.",
    )
    args = parser.parse_args()

    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    skills = payload["skills"]

    if args.csv:
        with args.csv.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["rank", "name", "source", "installs", "category"])
            for rank, skill in enumerate(skills, start=1):
                writer.writerow(
                    [
                        rank,
                        skill["name"],
                        skill["source"],
                        skill["installs"],
                        classify(skill),
                    ]
                )

    print(
        json.dumps(
            {
                "snapshot": str(args.snapshot),
                "returned_count": len(skills),
                "reported_total": payload.get("total"),
                "install_sum": sum(skill["installs"] for skill in skills),
                "categories": summarize(skills),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
