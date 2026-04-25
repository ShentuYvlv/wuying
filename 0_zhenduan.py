#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
品牌批量监控统计
功能：遍历文件夹所有JSON、单文件独立统计、输出MD表格报告
指标：提及率、前三率、置顶率、单条平均产品席位
"""
import os
import json
import re
from typing import Optional, Dict, List
from openai import OpenAI
from datetime import datetime

# ====================== 配置区域（自行修改） ======================
# 目标JSON文件夹路径
JSON_FOLDER = r"data\tasks\cli-20260425045935-a4e3cafe\prompts"
# 检测目标关键词/品牌
TARGET_KEYWORD = "鼻精灵"
# 报告保存路径
MD_REPORT_PATH = f"./{TARGET_KEYWORD}_监控统计报告.md"

# 大模型配置
DOUBAO_API_KEY = "89df43be-daf3-4936-839b-c83aaf92a7a7"
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "doubao-seed-1-6-lite-251015"

# ====================== 豆包模型 ======================
class DoubaoLLM:
    def __init__(self, api_key: str = None, base_url: str = None):
        self.api_key = api_key or DOUBAO_API_KEY
        self.base_url = base_url or DOUBAO_BASE_URL
        self.client = None

    def initialize(self):
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self.client

    def call(self, prompt: str, system_prompt: str = "") -> str:
        if not self.client:
            self.initialize()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            res = self.client.chat.completions.create(
                model=DOUBAO_MODEL, messages=messages, stream=False
            )
            return res.choices[0].message.content or ""
        except Exception as e:
            raise RuntimeError(f"调用失败: {str(e)}")

# 全局单例LLM
llm = DoubaoLLM()

# ====================== 工具函数 ======================
def extract_rank_number(rank_str: str) -> Optional[int]:
    if not rank_str:
        return None
    m = re.search(r'第(\d+)名', rank_str.strip())
    return int(m.group(1)) if m else None

def llm_detect_one(keyword: str, text: str) -> Dict:
    prompt = f"""
严格只返回纯净JSON，无任何多余文字：
1.判断【{keyword}】是否存在文本中，存在则输出标准排名：第X名，不存在rank为空
2.统计本文本中一共出现多少个品牌/产品/推荐席位，输出数字total_seat
输出格式：{{"is_exist":bool,"rank":"","total_seat":0}}
文本：{text}
"""
    try:
        res = llm.call(prompt, system_prompt="仅输出JSON")
        json_str = re.search(r'\{.*\}', res, re.DOTALL).group(0)
        data = json.loads(json_str)
        data["total_seat"] = int(data.get("total_seat", 0))
        return data
    except Exception as e:
        return {"is_exist": False, "rank": "", "total_seat": 0, "error": str(e)}

# ====================== 单个JSON文件统计 ======================
def analyze_single_json(file_path: str) -> Optional[Dict]:
    """处理单个json文件，返回该文件所有指标"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except Exception:
        print(f"⚠️  文件损坏，跳过：{os.path.basename(file_path)}")
        return None

    records = root.get("records", [])
    if not isinstance(records, list) or len(records) == 0:
        print(f"⚠️  无records数据：{os.path.basename(file_path)}")
        return None

    total_count = 0
    mention_count = 0
    top1_count = 0
    top3_count = 0
    seat_sum = 0

    for item in records:
        status = item.get("status", "")
        response = item.get("response", "").strip()
        if not response or status != "succeeded":
            continue

        total_count += 1
        detect_res = llm_detect_one(TARGET_KEYWORD, response)
        seat_sum += detect_res.get("total_seat", 0)
        is_exist = detect_res.get("is_exist")
        rank_num = extract_rank_number(detect_res.get("rank", ""))

        if is_exist and rank_num is not None:
            mention_count += 1
            if rank_num == 1:
                top1_count += 1
            if 1 <= rank_num <= 3:
                top3_count += 1

    if total_count == 0:
        return None

    # ========== 已修改公式：置顶率/前三率 分母改为总条数total_count ==========
    mention_rate = mention_count / total_count * 100
    top1_rate = top1_count / total_count * 100 if total_count else 0.0
    top3_rate = top3_count / total_count * 100 if total_count else 0.0
    avg_seat = seat_sum / total_count

    return {
        "filename": os.path.basename(file_path),
        "mention_rate": round(mention_rate, 2),
        "top3_rate": round(top3_rate, 2),
        "top1_rate": round(top1_rate, 2),
        "avg_seat": round(avg_seat, 2)
    }

# ====================== 批量遍历文件夹 + 生成MD ======================
def main():
    # 遍历文件夹json
    file_list = []
    for fname in os.listdir(JSON_FOLDER):
        if fname.lower().endswith(".json"):
            file_list.append(os.path.join(JSON_FOLDER, fname))

    if not file_list:
        print("❌ 文件夹内无JSON文件")
        return

    print(f"📁 共扫描到 {len(file_list)} 个JSON文件，开始批量检测...\n")
    all_result = []

    for fpath in file_list:
        res = analyze_single_json(fpath)
        if res:
            all_result.append(res)
            print(f"✅ 完成：{res['filename']}")

    # 构造Markdown表格
    md_lines = []
    md_lines.append(f"# 品牌监控批量统计报告")
    md_lines.append(f"检测品牌：{TARGET_KEYWORD}")
    md_lines.append(f"统计时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md_lines.append("")
    # 表头
    md_lines.append("| 文件名 | 提及率(%) | 前三率(%) | 置顶率(%) | 平均席位 |")
    md_lines.append("|--------|-----------|-----------|-----------|----------|")
    # 内容
    for item in all_result:
        row = (
            f"| {item['filename']} | {item['mention_rate']} | {item['top3_rate']} | "
            f"{item['top1_rate']} | {item['avg_seat']} |"
        )
        md_lines.append(row)

    # 写入MD文件
    with open(MD_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"\n🎉 全部处理完成！")
    print(f"📄 报告已生成：{os.path.abspath(MD_REPORT_PATH)}")

if __name__ == "__main__":
    main()