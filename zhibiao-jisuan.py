#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
品牌监控系统 - 豆包2.0-lite版本
核心功能：3种检测模式
1. 排名检测：判断品牌是否存在 + 提取排名
2. 正面提及：判断是否正面提及指定关键词
3. 负面提及：判断是否对关键词/产品出现负面表述
返回标准化JSON结果
"""
import json
import re
from typing import Optional, Dict
from openai import OpenAI

# ====================== 配置区域 ======================
DOUBAO_API_KEY = "89df43be-daf3-4936-839b-c83aaf92a7a7"
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "doubao-seed-1-6-lite-251015"

# ====================== 豆包模型调用 ======================
class DoubaoLLM:
    """豆包大模型调用类"""
    
    def __init__(self, api_key: str = None, base_url: str = None):
        self.api_key = api_key or DOUBAO_API_KEY
        self.base_url = base_url or DOUBAO_BASE_URL
        self.client = None
    
    def initialize(self):
        """初始化豆包客户端"""
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key
        )
        return self.client
    
    def call(self, prompt: str, system_prompt: str = "") -> str:
        """
        调用豆包模型
        
        Args:
            prompt (str): 用户提示词
            system_prompt (str): 系统提示词
            
        Returns:
            str: 模型输出结果
        """
        if not self.client:
            self.initialize()
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        try:
            response = self.client.chat.completions.create(
                model=DOUBAO_MODEL,
                messages=messages,
                stream=False
            )
            return response.choices[0].message.content if response.choices[0].message.content else ""
        except Exception as e:
            raise RuntimeError(f"豆包模型调用失败: {str(e)}")

# ====================== 数据读取 ======================
def load_json_response(file_path: str) -> Optional[str]:
    """读取JSON文件，提取response字段"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        response = data.get("response", "").strip()
        if not response:
            print("❌ 未找到response内容")
            return None
        print("✅ JSON文件读取成功")
        return response
    except Exception as e:
        print(f"❌ 读取文件失败：{str(e)}")
        return None

# ====================== 检测功能 ======================
def llm_detect(detect_type: str, keyword: str, response_text: str, llm: DoubaoLLM) -> Dict:
    """
    执行大模型检测
    
    Args:
        detect_type (str): rank/positive/negative 三种检测类型
        keyword (str): 检测关键词/品牌名
        response_text (str): 待检测文本
        llm (DoubaoLLM): 豆包模型实例
        
    Returns:
        Dict: 标准化JSON字典
    """
    # 构建检测提示词
    if detect_type == "rank":
        prompt = f"""
        严格执行任务，仅返回JSON，无其他任何内容：
        1. 判断品牌【{keyword}】是否存在于下方文本中
        2. 存在：is_exist=true，rank=第*名（严格第X名格式）
        3. 不存在：is_exist=false，rank=""
        4. 只输出JSON，禁止解释、文字、符号

        文本内容：{response_text}
        输出格式：{{"type":"rank","is_exist":true/false,"rank":"第*名"/""}}
        """
    elif detect_type == "positive":
        prompt = f"""
        严格执行任务，仅返回JSON，无其他任何内容：
        1. 判断下方文本中【是否正面提及关键词：{keyword}】
        2. 是：is_positive=true
        3. 否：is_positive=false
        4. 只输出JSON，禁止解释、文字、符号

        文本内容：{response_text}
        输出格式：{{"type":"positive","is_positive":true/false,"keyword":"{keyword}"}}
        """
    elif detect_type == "negative":
        prompt = f"""
        严格执行任务，仅返回JSON，无其他任何内容：
        1. 判断下方文本中【是否对关键词/产品：{keyword} 有负面评价、负面描述】
        2. 是：is_negative=true，reason=简要负面原因（无则空）
        3. 否：is_negative=false，reason=""
        4. 只输出JSON，禁止解释、文字、符号

        文本内容：{response_text}
        输出格式：{{"type":"negative","is_negative":true/false,"keyword":"{keyword}","reason":""}}
        """
    else:
        return {"error": "不支持的检测类型"}

    try:
        # 调用豆包模型
        res = llm.call(prompt, system_prompt="仅输出标准JSON，无任何额外内容")
        
        # 容错提取JSON
        json_match = re.search(r'\{.*\}', res.strip(), re.DOTALL)
        json_str = json_match.group(0) if json_match else res
        
        # 解析JSON
        result = json.loads(json_str)
        return result

    except Exception as e:
        error_result = {
            "type": detect_type,
            "error": f"大模型调用失败：{str(e)}"
        }
        return error_result

# ====================== 结果输出 ======================
def print_formatted_result(result: Dict):
    """格式化输出检测结果"""
    print("\n" + "="*60)
    detect_type = result.get("type")
    keyword = result.get("keyword", "")
    
    if detect_type == "rank":
        print(f"📊 检测类型：品牌排名检测")
        print(f"🔍 检测品牌：{keyword if keyword else '未指定'}")
        print(f"✅ 是否存在：{'是' if result.get('is_exist') else '否'}")
        print(f"🏆 品牌排名：{result.get('rank', '无')}")
        
    elif detect_type == "positive":
        print(f"😊 检测类型：正面提及检测")
        print(f"🔍 检测关键词：{keyword}")
        print(f"✅ 正面提及：{'是' if result.get('is_positive') else '否'}")
        
    elif detect_type == "negative":
        print(f"⚠️ 检测类型：负面提及检测")
        print(f"🔍 检测关键词/产品：{keyword}")
        print(f"❌ 存在负面：{'是' if result.get('is_negative') else '否'}")
        if result.get("reason"):
            print(f"📝 负面原因：{result.get('reason')}")

    if result.get("error"):
        print(f"❌ 错误信息：{result.get('error')}")
    print("="*60)

# ====================== 主程序 ======================
if __name__ == "__main__":
    # JSON文件路径
    JSON_PATH = "/Users/ggbond/Desktop/GEO-4-14-监控系统-test/yuanbao_acp-9wyg1c2u9suiag0j0_20260411T105446Z.json"

    # 初始化豆包模型
    llm = DoubaoLLM()

    # 1. 加载数据
    response_content = load_json_response(JSON_PATH)
    if not response_content:
        exit()

    # 2. 选择检测类型
    print("\n========== 请选择检测类型 ==========")
    print("1 → 品牌排名检测")
    print("2 → 正面提及检测")
    print("3 → 负面提及检测")
    choice = input("请输入数字(1/2/3)：").strip()

    type_map = {
        "1": "rank",
        "2": "positive",
        "3": "negative"
    }
    if choice not in type_map:
        print("❌ 输入错误，请输入1/2/3")
        exit()
    current_type = type_map[choice]

    # 3. 输入关键词/品牌
    tip = "请输入要检测的品牌名称：" if current_type == "rank" else "请输入要检测的关键词/产品名称："
    target_keyword = input(f"\n{tip}").strip()
    if not target_keyword:
        print("❌ 关键词不能为空")
        exit()

    # 4. 执行检测
    result = llm_detect(current_type, target_keyword, response_content, llm)

    # 5. 输出结果
    print_formatted_result(result)
