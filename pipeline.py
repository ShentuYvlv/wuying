#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 结果文件指标分析脚本

用途：
1. 读取当前爬虫生成的 data/tasks/*/prompts/*.json 文件
2. 对同一个平台 + prompt 下的多设备 response 做 LLM 判定
3. 输出这一整组数据唯一的一组汇总指标

说明：
- 一个 prompts/*.json 文件本来就只对应一个平台和一个 prompt
- 因此最终只会产出一组指标，而不是每条 record 各自产出一组
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI


DEFAULT_BASE_URL = os.getenv("PIPELINE_LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DEFAULT_MODEL = os.getenv("PIPELINE_LLM_MODEL", "doubao-seed-1-6-lite-251015")
DEFAULT_API_KEY = os.getenv("PIPELINE_LLM_API_KEY", "").strip()


class LLMClient:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise ValueError("Missing LLM API key. Set PIPELINE_LLM_API_KEY or pass --api-key.")
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def call_json(self, *, prompt: str, system_prompt: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        content = response.choices[0].message.content or ""
        return _extract_json_object(content)


def _extract_json_object(content: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", content.strip(), re.DOTALL)
    payload = match.group(0) if match else content.strip()
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON is not an object")
    return parsed


def empty_metrics() -> dict[str, float | None]:
    return {
        "提及率": None,
        "前三率": None,
        "置顶率": None,
        "负面提及率": None,
        "attitude": None,
    }


def load_prompt_records(file_path: str | Path) -> list[dict[str, Any]]:
    path = Path(file_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Prompt file must be a non-empty JSON array: {path}")
    records = [item for item in payload if isinstance(item, dict)]
    if not records:
        raise ValueError(f"Prompt file contains no valid record objects: {path}")
    return records


def detect_record(
    *,
    detect_type: str,
    keyword: str,
    response_text: str,
    llm: LLMClient,
) -> dict[str, Any]:
    if detect_type == "rank":
        prompt = f"""
你只返回 JSON，不要返回解释。

任务：
1. 判断品牌/关键词【{keyword}】是否在下方回答中被明确推荐或提及。
2. 如果被提及，判断它在推荐顺序里大致排第几。
3. 如果无法判断排名但能确认被提及，rank 填空字符串。

输出格式：
{{"type":"rank","is_exist":true,"rank":"第1名","reason":""}}
或
{{"type":"rank","is_exist":true,"rank":"","reason":"提及但无明确排序"}}
或
{{"type":"rank","is_exist":false,"rank":"","reason":"未提及"}}

回答内容：
{response_text}
""".strip()
    elif detect_type == "positive":
        prompt = f"""
你只返回 JSON，不要返回解释。

任务：
判断下方回答中，关键词【{keyword}】是否被正面提及。

输出格式：
{{"type":"positive","is_positive":true,"reason":""}}
或
{{"type":"positive","is_positive":false,"reason":"未正面提及"}}

回答内容：
{response_text}
""".strip()
    elif detect_type == "negative":
        prompt = f"""
你只返回 JSON，不要返回解释。

任务：
判断下方回答中，关键词【{keyword}】是否存在负面评价、负面描述、风险提示或明显贬义表述。

输出格式：
{{"type":"negative","is_negative":true,"reason":""}}
或
{{"type":"negative","is_negative":false,"reason":"未发现负面提及"}}

回答内容：
{response_text}
""".strip()
    else:
        raise ValueError(f"Unsupported detect type: {detect_type}")

    return llm.call_json(
        prompt=prompt,
        system_prompt="你是严格的结构化信息抽取器。只输出合法 JSON，不输出任何额外文字。",
    )


def parse_rank(rank_str: str | None) -> int | None:
    if not rank_str:
        return None
    match = re.search(r"第\s*(\d+)\s*名", rank_str)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\b", rank_str)
    return int(match.group(1)) if match else None


def calculate_rank_metrics(ranks: list[int | None]) -> dict[str, float | None]:
    total = len(ranks)
    if total == 0:
        return {
            **empty_metrics(),
            "提及率": 0.0,
            "前三率": 0.0,
            "置顶率": 0.0,
        }
    mentioned_count = sum(1 for rank in ranks if rank is not None)
    top3_count = sum(1 for rank in ranks if rank is not None and rank <= 3)
    top1_count = sum(1 for rank in ranks if rank is not None and rank == 1)
    return {
        "提及率": round(mentioned_count / total * 100, 2),
        "前三率": round(top3_count / total * 100, 2),
        "置顶率": round(top1_count / total * 100, 2),
        "负面提及率": None,
        "attitude": None,
    }


def calculate_boolean_metrics(
    values: list[bool],
    *,
    metric_name: str,
) -> dict[str, float | None]:
    total = len(values)
    rate = round((sum(1 for item in values if item) / total * 100), 2) if total else 0.0
    metrics: dict[str, float | None] = {
        **empty_metrics(),
    }
    metrics[metric_name] = rate
    return metrics


def analyze_prompt_records(
    *,
    records: list[dict[str, Any]],
    keyword: str,
    detect_type: str,
    llm: LLMClient,
    input_file: str | None = None,
) -> dict[str, Any]:
    if not records:
        raise ValueError("records cannot be empty")

    first = records[0]
    platform = str(first.get("platform") or "")
    platform_id = str(first.get("platform_id") or "")
    prompt = str(first.get("prompt") or first.get("query") or "")
    prompt_index = int(first.get("prompt_index") or 0)

    device_results: list[dict[str, Any]] = []
    rank_values: list[int | None] = []
    positive_values: list[bool] = []
    negative_values: list[bool] = []

    for index, record in enumerate(records, start=1):
        response_text = str(record.get("response") or "").strip()
        if not response_text:
            device_results.append(
                {
                    "index": index,
                    "device_id": record.get("device_id"),
                    "status": "skipped",
                    "error": "empty response",
                    "llm_result": None,
                    "parsed_rank": None,
                }
            )
            continue

        try:
            llm_result = detect_record(
                detect_type=detect_type,
                keyword=keyword,
                response_text=response_text,
                llm=llm,
            )
            parsed_rank = None
            if detect_type == "rank":
                parsed_rank = parse_rank(str(llm_result.get("rank") or ""))
                rank_values.append(parsed_rank if llm_result.get("is_exist") else None)
            elif detect_type == "positive":
                positive_values.append(bool(llm_result.get("is_positive")))
            elif detect_type == "negative":
                negative_values.append(bool(llm_result.get("is_negative")))

            device_results.append(
                {
                    "index": index,
                    "device_id": record.get("device_id"),
                    "instance_id": record.get("instance_id"),
                    "status": "processed",
                    "error": None,
                    "llm_result": llm_result,
                    "parsed_rank": parsed_rank,
                }
            )
        except Exception as exc:
            device_results.append(
                {
                    "index": index,
                    "device_id": record.get("device_id"),
                    "instance_id": record.get("instance_id"),
                    "status": "failed",
                    "error": str(exc),
                    "llm_result": None,
                    "parsed_rank": None,
                }
            )

    processed_count = sum(1 for item in device_results if item["status"] == "processed")
    failed_count = sum(1 for item in device_results if item["status"] == "failed")
    skipped_count = sum(1 for item in device_results if item["status"] == "skipped")

    if detect_type == "rank":
        metrics = calculate_rank_metrics(rank_values)
    elif detect_type == "positive":
        metrics = calculate_boolean_metrics(positive_values, metric_name="提及率")
    else:
        metrics = calculate_boolean_metrics(negative_values, metric_name="负面提及率")

    return {
        "input_file": input_file,
        "platform": platform,
        "platform_id": platform_id,
        "prompt": prompt,
        "prompt_index": prompt_index,
        "keyword": keyword,
        "detect_type": detect_type,
        "sample_count": len(records),
        "processed_count": processed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "metrics": metrics,
        "device_results": device_results,
    }


class PromptMetricsAnalyzer:
    def __init__(
        self,
        *,
        keyword: str,
        detect_type: str = "rank",
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.keyword = keyword.strip()
        self.detect_type = detect_type.strip().lower()
        if not self.keyword:
            raise ValueError("keyword cannot be empty")
        if self.detect_type not in {"rank", "positive", "negative"}:
            raise ValueError(f"Unsupported detect type: {self.detect_type}")
        self.llm = LLMClient(
            api_key=(api_key or DEFAULT_API_KEY).strip(),
            base_url=base_url,
            model=model,
        )

    def analyze_records(
        self,
        records: list[dict[str, Any]],
        *,
        input_file: str | None = None,
    ) -> dict[str, Any]:
        return analyze_prompt_records(
            records=records,
            keyword=self.keyword,
            detect_type=self.detect_type,
            llm=self.llm,
            input_file=input_file,
        )


def analyze_prompt_file(
    *,
    file_path: str | Path,
    keyword: str,
    detect_type: str,
    llm: LLMClient,
) -> dict[str, Any]:
    input_path = Path(file_path)
    records = load_prompt_records(input_path)
    return analyze_prompt_records(
        records=records,
        keyword=keyword,
        detect_type=detect_type,
        llm=llm,
        input_file=str(input_path),
    )


def default_output_path(file_path: str | Path, detect_type: str, keyword: str) -> Path:
    input_path = Path(file_path)
    safe_keyword = _safe_filename_part(keyword)[:40]
    return input_path.with_name(
        input_path.stem + f"_{detect_type}_{safe_keyword}_analysis.json"
    )


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze one prompts/*.json file and compute one metric group.")
    parser.add_argument("--file", required=True, help="Path to one prompts/*.json file")
    parser.add_argument("--keyword", required=True, help="Brand/keyword to detect")
    parser.add_argument(
        "--detect-type",
        choices=["rank", "positive", "negative"],
        default="rank",
        help="Detection mode. Default: rank",
    )
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="LLM API key")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="LLM base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model name")
    parser.add_argument("--output", help="Optional summary output path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    llm = LLMClient(api_key=args.api_key, base_url=args.base_url, model=args.model)

    summary = analyze_prompt_file(
        file_path=args.file,
        keyword=args.keyword,
        detect_type=args.detect_type,
        llm=llm,
    )
    output_path = Path(args.output) if args.output else default_output_path(args.file, args.detect_type, args.keyword)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
    print(f"saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
