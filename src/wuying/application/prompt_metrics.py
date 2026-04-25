from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from openai import OpenAI


def _normalize_keyword(value: str) -> str:
    keyword = value.strip()
    if len(keyword) >= 2 and keyword[0] == keyword[-1] and keyword[0] in {'"', "'"}:
        keyword = keyword[1:-1].strip()
    return keyword


def _extract_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response.")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Model response JSON is not an object.")
    return payload


def _extract_rank_number(rank_str: str) -> int | None:
    if not rank_str:
        return None
    match = re.search(r"第\s*(\d+)\s*名", rank_str.strip())
    if not match:
        return None
    return int(match.group(1))


class PromptMetricsAnalyzer:
    def __init__(
        self,
        *,
        keyword: str,
        detect_type: str = "rank",
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "doubao-seed-1-6-lite-251015",
    ) -> None:
        normalized_keyword = _normalize_keyword(keyword)
        if not normalized_keyword:
            raise ValueError("keyword cannot be empty")
        if not api_key:
            raise ValueError("api_key is required for PromptMetricsAnalyzer")
        self.keyword = normalized_keyword
        self.detect_type = detect_type.strip().lower() or "rank"
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def analyze_records(
        self,
        records: list[dict[str, Any]],
        *,
        input_file: str = "",
    ) -> dict[str, Any]:
        valid_records = [
            record
            for record in records
            if str(record.get("status") or "").strip().lower() == "succeeded"
            and str(record.get("response") or "").strip()
        ]
        if not valid_records:
            return {
                "input_file": input_file,
                "keyword": self.keyword,
                "record_count": 0,
                "metrics": {
                    "提及率": 0.0,
                    "前三率": 0.0,
                    "置顶率": 0.0,
                    "负面提及率": None,
                    "attitude": None,
                },
                "details": [],
            }

        mention_count = 0
        top3_count = 0
        top1_count = 0
        details: list[dict[str, Any]] = []

        for record in valid_records:
            detection = self._detect_one(str(record.get("response") or ""))
            rank_number = _extract_rank_number(str(detection.get("rank") or ""))
            is_exist = bool(detection.get("is_exist")) and rank_number is not None
            if is_exist:
                mention_count += 1
                if rank_number == 1:
                    top1_count += 1
                if rank_number <= 3:
                    top3_count += 1
            details.append(
                {
                    "device_id": record.get("device_id"),
                    "is_exist": is_exist,
                    "rank": detection.get("rank") or "",
                    "total_seat": detection.get("total_seat"),
                }
            )

        total_count = len(valid_records)
        return {
            "input_file": input_file,
            "keyword": self.keyword,
            "record_count": total_count,
            "metrics": {
                "提及率": round(mention_count / total_count * 100, 2),
                "前三率": round(top3_count / total_count * 100, 2),
                "置顶率": round(top1_count / total_count * 100, 2),
                "负面提及率": None,
                "attitude": None,
            },
            "details": details,
        }

    def _detect_one(self, text: str) -> dict[str, Any]:
        prompt = (
            f"严格只返回纯净JSON，无任何多余文字。\n"
            f"目标关键词：{self.keyword}\n"
            "任务：\n"
            "1. 判断目标关键词是否出现在推荐结果里。\n"
            "2. 如果出现，输出它的标准排名，格式必须是“第X名”；如果未出现，rank 为空字符串。\n"
            "3. 统计这段文本一共提到了多少个品牌、产品或推荐席位，输出 total_seat。\n"
            '输出格式：{"is_exist":true,"rank":"第1名","total_seat":3}\n'
            f"文本：{text}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个严格的结构化信息抽取器，只输出JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        content = response.choices[0].message.content or ""
        payload = _extract_json_object(content)
        return {
            "is_exist": bool(payload.get("is_exist")),
            "rank": str(payload.get("rank") or "").strip(),
            "total_seat": int(payload.get("total_seat") or 0),
        }


def _load_prompt_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
    raise ValueError(f"Invalid prompt file payload: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze prompt result JSON files and compute metrics.")
    parser.add_argument("path", help="Prompt JSON file or directory")
    parser.add_argument("--keyword", required=True, help="Brand / keyword to detect")
    parser.add_argument("--api-key", required=True, help="LLM API key")
    parser.add_argument("--base-url", default="https://ark.cn-beijing.volces.com/api/v3")
    parser.add_argument("--model", default="doubao-seed-1-6-lite-251015")
    args = parser.parse_args()

    analyzer = PromptMetricsAnalyzer(
        keyword=args.keyword,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )

    target = Path(args.path)
    files = [target] if target.is_file() else sorted(target.glob("*.json"))
    results: list[dict[str, Any]] = []
    for file_path in files:
        records = _load_prompt_file(file_path)
        results.append(analyzer.analyze_records(records, input_file=str(file_path)))

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
