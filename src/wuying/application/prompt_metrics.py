from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
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


def _normalize_negative_words(words: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for word in words or []:
        cleaned = str(word or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


class PromptMetricsAnalyzer:
    def __init__(
        self,
        *,
        keyword: str,
        detect_type: str = "rank",
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "doubao-seed-1-6-lite-251015",
        negative_words: list[str] | None = None,
        task_type: str = "normal_monitor",
    ) -> None:
        normalized_keyword = _normalize_keyword(keyword)
        if not normalized_keyword:
            raise ValueError("keyword cannot be empty")
        if not api_key:
            raise ValueError("api_key is required for PromptMetricsAnalyzer")
        self.keyword = normalized_keyword
        self.task_type = (task_type or "normal_monitor").strip().lower() or "normal_monitor"
        self.detect_type = detect_type.strip().lower() or "rank"
        self.negative_words = _normalize_negative_words(negative_words)
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
                "task_type": self.task_type,
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

        if self.detect_type in {"negative", "negative_word", "negative_words", "brand_negative"}:
            return self._analyze_negative_records(valid_records, input_file=input_file)

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
            "task_type": self.task_type,
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

    def _analyze_negative_records(
        self,
        records: list[dict[str, Any]],
        *,
        input_file: str,
    ) -> dict[str, Any]:
        total_count = len(records)
        brand_normal_count = 0
        brand_abnormal_count = 0
        negative_any_count = 0
        negative_word_counts: defaultdict[str, int] = defaultdict(int)
        details: list[dict[str, Any]] = []

        for record in records:
            query = str(record.get("query") or record.get("prompt") or "")
            response = str(record.get("response") or "")
            brand_detection = self._judge_brand_unified(query=query, response=response)
            brand_normal = bool(brand_detection.get("brand_normal"))
            if brand_normal:
                brand_normal_count += 1
            else:
                brand_abnormal_count += 1

            negative_detection = (
                self._detect_brand_negative(response)
                if self.negative_words
                else {
                    "has_negative": False,
                    "hit_words": [],
                    "analysis_desc": "negative words not configured",
                    "related_sentences": [],
                }
            )
            hit_words = [
                word for word in negative_detection.get("hit_words", [])
                if isinstance(word, str) and word in self.negative_words
            ]
            has_negative = bool(negative_detection.get("has_negative")) and bool(hit_words)
            if has_negative:
                negative_any_count += 1
            for word in hit_words:
                negative_word_counts[word] += 1

            details.append(
                {
                    "device_id": record.get("device_id"),
                    "brand_normal": brand_normal,
                    "brand_analysis_desc": brand_detection.get("analysis_desc") or "",
                    "brand_abnormal_detail": brand_detection.get("abnormal_detail") or "",
                    "has_negative": has_negative,
                    "hit_words": hit_words,
                    "negative_analysis_desc": negative_detection.get("analysis_desc") or "",
                    "related_sentences": negative_detection.get("related_sentences") or [],
                }
            )

        normal_rate = round(brand_normal_count / total_count * 100, 2)
        abnormal_rate = round(brand_abnormal_count / total_count * 100, 2)
        negative_rate = round(negative_any_count / total_count * 100, 2)
        negative_word_stats = {
            word: {
                "hit_count": negative_word_counts[word],
                "hit_rate": round(negative_word_counts[word] / total_count * 100, 2),
            }
            for word in self.negative_words
        }
        return {
            "input_file": input_file,
            "keyword": self.keyword,
            "task_type": self.task_type,
            "detect_type": self.detect_type,
            "negative_words": self.negative_words,
            "record_count": total_count,
            "metrics": {
                "提及率": normal_rate,
                "前三率": None,
                "置顶率": None,
                "负面提及率": negative_rate,
                "attitude": None,
            },
            "brand": {
                "normal_count": brand_normal_count,
                "normal_rate": normal_rate,
                "abnormal_count": brand_abnormal_count,
                "abnormal_rate": abnormal_rate,
            },
            "negative_word_stats": negative_word_stats,
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

    def _judge_brand_unified(self, *, query: str, response: str) -> dict[str, Any]:
        prompt = f"""
# 任务背景
用户问句：{query}
回答内容：{response}
监控目标品牌：{self.keyword}

# 分析步骤
步骤1：通读用户问题和回答，理清对话主题、品类、涉及所有品牌。
步骤2：综合判断回答对【{self.keyword}】这个品牌的整体处理情况。
判定为【异常】包含以下所有情况：
1. 完全没有提及该品牌
2. 把其他品牌错误当成 {self.keyword}
3. 把 {self.keyword} 归错品类、概念混淆
4. 理解偏差、答非所问、歪曲品牌含义

判定为【正常】只有一种：
回答准确识别、正确提及、没有混淆、没有认错该品牌。

# 输出格式
严格只返回纯净JSON：
{{
  "brand_normal": true,
  "analysis_desc": "推理分析过程",
  "abnormal_detail": "异常原因，正常则填空字符串"
}}
"""
        response_payload = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个严格的品牌语义判定器，只输出JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        content = response_payload.choices[0].message.content or ""
        payload = _extract_json_object(content)
        return {
            "brand_normal": bool(payload.get("brand_normal")),
            "analysis_desc": str(payload.get("analysis_desc") or ""),
            "abnormal_detail": str(payload.get("abnormal_detail") or ""),
        }

    def _detect_brand_negative(self, response: str) -> dict[str, Any]:
        prompt = f"""
# 任务
目标监控品牌：{self.keyword}
待监控负面词列表：{self.negative_words}
待分析回答内容：{response}

# 强制分析步骤
步骤1：先读懂整段回答语义。
步骤2：逐个审核负面词，只有专门用来评价、吐槽、形容 {self.keyword} 品牌本身才算命中。
步骤3：以下一律不算：
- 形容其他品牌
- 形容整个品类泛称
- 举例、反问、引用，非真实负面评价

# 输出格式
严格只返回纯净JSON：
{{
  "has_negative": true,
  "hit_words": ["命中负面词列表"],
  "analysis_desc": "逐词分析理由",
  "related_sentences": ["对应原文句子"]
}}
"""
        response_payload = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个严格的品牌负面语义判定器，只输出JSON对象。"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
        )
        content = response_payload.choices[0].message.content or ""
        payload = _extract_json_object(content)
        hit_words = payload.get("hit_words")
        related_sentences = payload.get("related_sentences")
        return {
            "has_negative": bool(payload.get("has_negative")),
            "hit_words": [str(item).strip() for item in hit_words if str(item).strip()]
            if isinstance(hit_words, list)
            else [],
            "analysis_desc": str(payload.get("analysis_desc") or ""),
            "related_sentences": [
                str(item).strip() for item in related_sentences if str(item).strip()
            ]
            if isinstance(related_sentences, list)
            else [],
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
    parser.add_argument("--detect-type", default="rank", choices=["rank", "negative"])
    parser.add_argument("--negative-words", default="", help="Comma-separated negative words for negative tasks")
    args = parser.parse_args()

    analyzer = PromptMetricsAnalyzer(
        keyword=args.keyword,
        detect_type=args.detect_type,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        negative_words=[
            item.strip()
            for item in re.split(r"[,，;；\n]+", args.negative_words)
            if item.strip()
        ],
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
