import argparse
import os
import re
import time
import hashlib #哈希去重
import requests # type: ignore
import pdfplumber # type: ignore
from typing import List, Tuple
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter # type: ignore
from urllib3.util.retry import Retry # type: ignore
from init import get_config # type: ignore
from nltk.tokenize import sent_tokenize # type: ignore

config = get_config()

# 使用配置参数
PDF_DIR = config.PDF_DIR  # PDF 文件保存目录
RESULT_DIR = config.RESULT_DIR  # 分析结果保存目录
SEARCH_DIR = config.SEARCH_DIR  # 论文检索结果保存目录
Path = config.Path  #输入目录路径（包含论文链接文件）
Timeout = config.TIMEOUT  #超时时间

MAX_RETRIES = config.MAX_RETRIES # 最大重试次数
BACKOFF_FACTOR = config.BACKOFF_FACTOR # 超时回退系数
MAX_PDF_PAGES = config.MAX_PDF_PAGES  # 最大解析页数
CHUNK_SIZE = config.CHUNK_SIZE   # 文本分块长度

API_KEY = config.API_KEY  # API 密钥
API_URL = config.API_URL  # API 地址
MODEL_ID = config.MODEL_ID  # 模型 ID



def setup_requests_session():
    """配置带重试机制的请求会话"""
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    return session

def extract_arxiv_id(url: str) -> str:
    """从arXiv URL提取论文ID（兼容版本号）"""
    pattern = r"arxiv\.org/(abs|pdf)/([\d\.v]+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"无效的arXiv链接: {url}")
    return match.group(2).split('.pdf')[0]

def download_pdf(url: str, save_path: str) -> bool:
    """下载PDF文件到指定路径（带进度显示）"""
    try:
        response = requests.get(url, stream=True, timeout=Timeout)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(1024*1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    progress = downloaded / total_size * 100
                    print(f"\r原始PDF文件下载进度: {progress:.1f}%", end='')
        print()
        return True
    except Exception as e:
        print(f"\n下载失败 {url}: {str(e)}")
        return False

def detect_section_change(page) -> bool:
    """检测章节标题变化"""
    # 基于字体特征检测标题（示例实现）
    large_fonts = [char["size"] for char in page.chars if char["size"] > 14]
    if len(large_fonts) > 3:
        return True
    # 基于关键词检测
    text = page.extract_text()
    if re.search(r'\b(Abstract|Introduction|Method|References)\b', text):
        return True
    return False

def detect_section_change(page, content) -> bool:
    """增强章节检测逻辑"""
    # 基于字体特征检测
    large_chars = [c for c in page.chars if c["size"] > 14]
    if len(large_chars) > 5 and any(c["text"].isupper() for c in large_chars):
        return True
    
    # 基于内容模式检测
    section_pattern = r'''
        ^\s*                # 起始空白
        (?:                 
            \d+             # 数字编号
            [\.\s]+         # 分隔符
            [A-Z]{3,}       # 大写标题单词
        |  
            [A-Z]{3,}       # 纯大写标题
        )
        \b
    '''
    return re.search(section_pattern, content, re.X) is not None

def extract_pdf_text(pdf_path: str) -> List[str]:
    """分块提取PDF文本"""
    chunks = []
    current_chunk = []
    current_length = 0
    processed_hashes = set()  # 新增重复内容检测
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[:MAX_PDF_PAGES]):
                # 增强布局参数配置
                text = page.filter(
                    lambda obj: obj["object_type"] == "char" and obj["size"] > 8
                ).extract_text(
                    layout=True,
                    x_tolerance=2,
                    y_tolerance=3,
                    keep_blank_chars=False
                ) or ""

                # 增强型文本清洗管道
                clean_content = re.sub(r'(?<=\b)([A-Z])\s(?=[A-Z]\b)', r'\1', text)  # 修复大写单词分割
                clean_content = re.sub(r'(\d)\s*-\s*(\d)', r'\1-\2', clean_content)  # 保留数字连字符
                clean_content = re.sub(r'\s([\(\{\[\]\}\)])', r'\1', clean_content)  # 修复括号粘连
                clean_content = re.sub(r'([A-Za-z])\s+(?=\d)', r'\1', clean_content)  # 修复字母数字粘连
                clean_content = re.sub(r'\s{2,}', ' ', clean_content).strip()

                # 新增重复内容检测
                content_hash = hashlib.md5(clean_content.encode()).hexdigest()
                if content_hash in processed_hashes:
                    continue
                processed_hashes.add(content_hash)

                # 增强章节检测
                if detect_section_change(page, clean_content):
                    if current_chunk:
                        chunks.append(' '.join(current_chunk))
                        current_chunk = []
                        current_length = 0
                    chunks.append(clean_content)  # 章节标题独立分块
                    continue

                # 智能分块逻辑
                sentences = sent_tokenize(clean_content)
                for sent in sentences:
                    words = re.findall(r'\b\w+[\-/]?\w*\b|[\(\)\{\}\[\]]', sent)  # 增强单词分割
                    
                    for word in words:
                        estimated_length = current_length + len(word) + 1
                        
                        # 动态分块策略（允许±15%浮动）
                        if estimated_length > CHUNK_SIZE * 1.15:
                            if len(current_chunk) > CHUNK_SIZE * 0.3:
                                chunks.append(' '.join(current_chunk))
                                current_chunk = [word]
                                current_length = len(word)
                            else:
                                current_chunk.append(word)
                                current_length += len(word) + 1
                        else:
                            current_chunk.append(word)
                            current_length += len(word) + 1

                    # 句子完整性保护
                    if current_chunk and current_length > CHUNK_SIZE * 0.8:
                        chunks.append(' '.join(current_chunk))
                        current_chunk = []
                        current_length = 0

        return chunks

    except Exception as e:
        print(f"解析PDF失败 {pdf_path}: {str(e)}")
        return []

def process_chunk(session, chunk: str, url: str, chunk_num: int, total_chunks: int) -> str:
    """处理单个文本块"""
    prompt = f"""作为计算机科学领域研究员，请基于以下材料撰写论文分析报告（来自{url})：
                {chunk[:CHUNK_SIZE]}
                由于我提供给你的论文材料的一小块分块材料，当前进度：{chunk_num}/{total_chunks}，故请你首先判断该块属于论文的哪一内容部分
                （例如"abstract", "introduction", "method", "experiment", "conclusion"……等等关键章节），以及主要内容是什么，然后请你
                根据判断结果选取下面要点组织内容，无须全部写全，根据判断得到的结果进行有倾向性的总结即可（例如摘要部分可以着重总结“核心问题解析”，而method部分可以着重总结“方法创新”
                请严格按以下结构与你自己的判断有倾向性地选取结构中的部分进行组织内容（使用中文Markdown格式），注意需要在开头先对该分块进行一定的判断归类，说明其主要内容：
                ## 核心问题解析
                    1. **研究动机**：用"问题三角"框架说明：
                    - 领域现状：当前领域的主流方法
                    - 现存缺陷：现有方法的3个局限性
                    - 本文目标：拟解决的关键问题
                    2. **可行性论证**：分析论文中提出的理论/技术可行性证据
                ## 方法创新
                    1. **技术路线图**：用流程图描述整体框架（文字形式）
                    2. **核心创新点**（对比分析）：
                    | 对比维度 | 传统方法 | 本文方法 | 优势分析 |
                    |---------|---------|---------|---------|
                    | [维度1] | ...     | ...     | ...     |
                    | [维度2] | ...     | ...     | ...     |
                    3. **关键公式**：
                    - 公式1：$$...$$ （说明物理意义）
                    - 公式2：$$...$$ （解释创新点）
                ## 实验验证
                    1. **实验设置**：
                    - 数据集构成（训练/测试比例）
                    - 基线模型选择逻辑
                    - 评估指标合理性分析
                    2. **关键结果**：
                    - 主实验结果（附重要数据表格）
                    - 消融实验结论
                    - 计算效率对比（如FLOPS/内存占用）
                ## 学术贡献
                    1. **理论贡献**（分点说明）
                    2. **实践价值**（对工业界的影响）
                ## 批判性分析
                    1. **方法局限性**（分点说明）
                    2. **改进建议**（提出可行方向）
                    3. **可复现性**：根据论文描述评估复现难度
                ## 延伸思考
                    1. **关联工作**：列出3篇相关论文（非本文参考文献）并说明关联性
                    2. **应用场景**：预测可能产生影响的3个领域
                【写作要求】
                    1. 专业术语中英对照（如：注意力机制, Attention Mechanism）
                    2. 重要结论需标注PDF出处（例：见PDF P5, Section 3.2）
                    3. 避免直接翻译原文，需体现分析深度
                    4. 争议性观点需标注"需进一步验证"
                    """

    try:
        response = session.post(
            url = API_URL,
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 16384
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout = Timeout 
        )
        response.raise_for_status()

        print(f"当前进度：{chunk_num}/{total_chunks}")

        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"分块处理失败: {str(e)}")
        return ""

def generate_final_summary(session, chunks: List[str], url: str) -> str:
    """生成最终汇总报告"""
    summary_prompt = f"""作为计算机科学领域研究员，请根据以下全部的论文分析片段分块汇总，对该论文给出全文的总结：
                        论文地址: {url}
                        分析片段:
                        {'-'*40}
                        {'\n\n'.join(chunks)}
                        {'-'*40}

                        请严格按以下结构组织内容（使用中文Markdown格式）：

                        ## 核心问题解析
                        1. **研究动机**：用"问题三角"框架说明：
                        - 领域现状：当前领域的主流方法
                        - 现存缺陷：现有方法的3个局限性
                        - 本文目标：拟解决的关键问题
                        2. **可行性论证**：分析论文中提出的理论/技术可行性证据

                        ## 方法创新
                        1. **技术路线图**：用流程图描述整体框架（文字形式）
                        2. **核心创新点**（对比分析）：
                        | 对比维度 | 传统方法 | 本文方法 | 优势分析 |
                        |---------|---------|---------|---------|
                        | [维度1] | ...     | ...     | ...     |
                        | [维度2] | ...     | ...     | ...     |
                        3. **关键公式**（至少2个）：
                        - 公式1：$$...$$ （说明物理意义）
                        - 公式2：$$...$$ （解释创新点）

                        ## 实验验证
                        1. **实验设置**：
                        - 数据集构成（训练/测试比例）
                        - 基线模型选择逻辑
                        - 评估指标合理性分析
                        2. **关键结果**：
                        - 主实验结果（附重要数据表格）
                        - 消融实验结论
                        - 计算效率对比（如FLOPS/内存占用）

                        ## 学术贡献
                        1. **理论贡献**（分点说明）
                        2. **实践价值**（对工业界的影响）

                        ## 批判性分析
                        1. **方法局限性**（分3点说明）
                        2. **改进建议**（提出2条可行方向）
                        3. **可复现性**：根据论文描述评估复现难度

                        ## 延伸思考
                        1. **关联工作**：列出3篇相关论文（非本文参考文献）并说明关联性
                        2. **应用场景**：预测可能产生影响的3个领域

                        【写作要求】
                        1. 专业术语中英对照（如：注意力机制, Attention Mechanism）
                        2. 重要结论需标注PDF出处（例：见PDF P5, Section 3.2）
                        3. 避免直接翻译原文，需体现分析深度
                        4. 争议性观点需标注"需进一步验证"
                        """

    try:
        response = session.post(
            url = API_URL,
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": summary_prompt}],
                "temperature": 0.2,
                "max_tokens": 16384
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout = Timeout
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"汇总失败: {str(e)}")
        return "生成完整摘要失败，请查看分块分析结果"

def process_paper(session, filename: str, url: str, result_dir: str):
    """处理单篇论文"""
    try:
        # 下载PDF
        arxiv_id = extract_arxiv_id(url)
        pdf_path = os.path.join(PDF_DIR, f"{arxiv_id}.pdf")
        
        if not os.path.exists(pdf_path) and not download_pdf(f"https://arxiv.org/pdf/{arxiv_id}.pdf", pdf_path):
            return

        # 分块处理
        text_chunks = extract_pdf_text(pdf_path)
        if not text_chunks:
            print("未提取到有效文本")
            return
        else:
            print("已成功预处理目标论文块，即将调用模型进行处理，该过程与远端api响应速度相关，请稍等")

        # 处理分块
        chunk_results = []
        for idx, chunk in enumerate(text_chunks, 1):
            result = process_chunk(session, chunk, url, idx, len(text_chunks))
            chunk_results.append(result)
            # print(chunk+"\n\n")
            time.sleep(1)  # 请求间隔

        # 生成汇总
        final_summary = generate_final_summary(session, chunk_results, url)

        # 保存结果
        md_filename = os.path.splitext(filename)[0] + ".md"
        output_part_path = os.path.join(result_dir, os.path.basename("/part"))
        os.makedirs(output_part_path, exist_ok=True)
        output_part_path = os.path.join(output_part_path, os.path.basename(md_filename))

        output_sum_path = os.path.join(result_dir, os.path.basename("/sum"))
        os.makedirs(output_sum_path, exist_ok=True)
        output_sum_path = os.path.join(output_sum_path, os.path.basename(md_filename))
        
        #保存分块结果
        with open(output_part_path, "w", encoding="utf-8") as f:
            f.write(f"# 论文分块分析报告\n\n")
            f.write(f"## 原文信息\n- 地址: [{url}]({url})\n")
            f.write(f"## 分块分析\n")
            for i, res in enumerate(chunk_results, 1):
                f.write(f"\n### 片段 {i}\n{res}\n")
        
        #保存全文结果
        with open(output_sum_path, "w", encoding="utf-8") as f:
            f.write(f"# 论文全文分析报告\n\n")
            f.write(f"## 原文信息\n- 地址: [{url}]({url})\n")
            f.write(f"\n## \n{final_summary}")
            
        print(f"成功保存分块处理结果至{output_part_path}\n成功保存全文分析结果至{output_sum_path}")

    except Exception as e:
        print(f"处理失败: {str(e)}")

if __name__ == "__main__":    
    # 初始化环境
    os.makedirs(PDF_DIR, exist_ok=True)
    result_dir = os.path.join(RESULT_DIR, os.path.basename(Path.rstrip("/\\")))
    os.makedirs(result_dir, exist_ok=True)

    # 创建会话
    session = setup_requests_session()

    # 处理文件
    files = [f for f in os.listdir(Path) if f.endswith(".txt")]
    for idx, filename in enumerate(files, 1):
        print(f"\n即将处理第{idx}/{len(files)}篇目标论文: {filename}")  
        with open(os.path.join(Path, filename), "r", encoding="utf-8") as f:
            url = f.readline().strip()
            process_paper(session, filename, url, result_dir)