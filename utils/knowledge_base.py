import os
import json
import re
from typing import List, Dict, Optional, Tuple
import lancedb
from lancedb.pydantic import LanceModel, Vector
from utils.logger import logger

# 使用fastembed进行语义检索
try:
    from fastembed import TextEmbedding
    USE_FASTEMBED = True
except ImportError as e:
    logger.warning(f"⚠️ 无法导入fastembed，将使用TF-IDF方案: {e}")
    USE_FASTEMBED = False

if not USE_FASTEMBED:
    from sklearn.feature_extraction.text import TfidfVectorizer
    import numpy as np


class QADocument(LanceModel):
    """QA文档数据模型"""
    id: str
    q_id: str
    content: str
    question: str
    answer: str
    vector: Vector(384)


class KnowledgeBase:
    """基于LanceDB的知识库管理类"""
    
    def __init__(self, db_path: str = "./data/lancedb"):
        """
        初始化知识库
        
        Args:
            db_path: 数据库存储路径
        """
        self.db_path = db_path
        self.vector_dim = 384
        
        # 确保数据目录存在
        os.makedirs(db_path, exist_ok=True)
        
        # 初始化嵌入模型
        logger.info(f"📚 初始化语义嵌入模型...")
        use_fastembed_local = USE_FASTEMBED
        
        if use_fastembed_local:
            try:
                logger.info(f"🔄 正在加载/下载模型: BAAI/bge-small-en-v1.5")
                self.embedding_model = TextEmbedding(
                    model_name="BAAI/bge-small-en-v1.5",
                    cache_dir=None,
                    threads=1
                )
                # 强制触发模型加载和下载
                _ = self.embedding_model.embed(["test"])
                self.vector_dim = 384
                logger.info(f"✅ 使用fastembed模型: BAAI/bge-small-en-v1.5")
            except Exception as e:
                logger.error(f"❌ 加载fastembed失败，回退到TF-IDF")
                logger.error(f"   错误详情: {str(e)[:200]}")
                logger.warning(f"⚠️ 提示：如果是首次使用，可能需要手动下载模型")
                logger.warning(f"⚠️ 可以尝试命令：python -c \"from fastembed import TextEmbedding; model = TextEmbedding('BAAI/bge-small-en-v1.5'); print('下载完成')\"")
                use_fastembed_local = False
        
        self.use_fastembed = use_fastembed_local
        
        if not self.use_fastembed:
            self.tfidf = TfidfVectorizer(
                max_features=384,
                ngram_range=(1, 2),
                token_pattern=r'(?u)\b\w+\b'
            )
            self._fit_tfidf()
            logger.info(f"✅ 使用TF-IDF嵌入方案")
        
        # 连接LanceDB
        logger.info(f"📊 连接LanceDB: {db_path}")
        self.db = lancedb.connect(db_path)
        
        # 创建或获取表
        self.table_name = "qa_documents"
        if self.table_name not in self.db.table_names():
            logger.info(f"🗂️ 创建新表: {self.table_name}")
            self.db.create_table(self.table_name, schema=QADocument)
        
        self.table = self.db.open_table(self.table_name)
        logger.info(f"✅ 知识库初始化完成")
    
    def _fit_tfidf(self):
        """预训练TF-IDF模型（使用示例文本）"""
        sample_texts = [
            "你好", "喜欢猫", "喜欢狗", "年龄多大", "来自哪里",
            "直播时间", "唱歌", "最喜欢的颜色", "性格怎么样",
            "户川纯", "BLAME漫画", "太空频道5", "学校", "体育",
            "初恋", "约会", "表白", "打工", "休息", "目标"
        ]
        self.tfidf.fit(sample_texts)
    
    def _generate_embedding(self, text: str) -> List[float]:
        """生成文本嵌入向量"""
        if self.use_fastembed:
            # 使用fastembed进行语义嵌入
            embeddings = list(self.embedding_model.embed([text]))
            return embeddings[0].tolist()
        else:
            # TF-IDF备选方案
            vector = self.tfidf.transform([text]).toarray()[0]
            if len(vector) < 384:
                vector = np.pad(vector, (0, 384 - len(vector)), mode='constant')
            elif len(vector) > 384:
                vector = vector[:384]
            return vector.tolist()
    
    def add_qa_pair(self, q_id: str, question: str, answer: str) -> None:
        """
        添加单个QA对
        
        Args:
            q_id: 问题ID（如Q07）
            question: 问题内容
            answer: 回答内容
        """
        content = f"Q: {question}\nA: {answer}"
        vector = self._generate_embedding(content)
        
        doc = QADocument(
            id=f"{q_id}_{hash(content) % 1000000}",
            q_id=q_id,
            content=content,
            question=question,
            answer=answer,
            vector=vector
        )
        
        self.table.add([doc])
        logger.debug(f"📥 添加QA文档: {q_id}")
    
    def add_qa_pairs(self, qa_list: List[Tuple[str, str, str]]) -> None:
        """
        批量添加QA对
        
        Args:
            qa_list: QA三元组列表，格式为[(q_id, question, answer), ...]
        """
        documents = []
        for q_id, question, answer in qa_list:
            content = f"Q: {question}\nA: {answer}"
            vector = self._generate_embedding(content)
            
            doc = QADocument(
                id=f"{q_id}_{hash(content) % 1000000}",
                q_id=q_id,
                content=content,
                question=question,
                answer=answer,
                vector=vector
            )
            documents.append(doc)
        
        if documents:
            self.table.add(documents)
            logger.info(f"📥 批量添加 {len(documents)} 条QA文档")
    
    def query(self, query_text: str, top_k: int = 3) -> List[Dict[str, str]]:
        """
        语义检索相关QA
        
        Args:
            query_text: 查询文本
            top_k: 返回前k个结果
            
        Returns:
            匹配的QA列表，包含 q_id, question, answer, content
        """
        query_vector = self._generate_embedding(query_text)
        
        results = self.table.search(query_vector)\
            .limit(top_k)\
            .to_list()
        
        return [
            {
                "q_id": item.get("q_id") if isinstance(item, dict) else item.q_id,
                "question": item.get("question") if isinstance(item, dict) else item.question,
                "answer": item.get("answer") if isinstance(item, dict) else item.answer,
                "content": item.get("content") if isinstance(item, dict) else item.content
            }
            for item in results
        ]
    
    def query_with_keyword(self, query_text: str, keywords: Dict[str, List[str]], top_k: int = 3) -> List[Dict[str, str]]:
        """
        双路检索：关键词匹配优先（权重最高），语义检索作为补充
        
        Args:
            query_text: 查询文本
            keywords: 关键词映射字典，格式为 {"关键词": ["Q01", "Q02"], ...}
            top_k: 返回前k个结果
            
        Returns:
            匹配的QA列表（关键词匹配结果优先，且带有matched_by标记）
        """
        # 1. 关键词触发（Hard Match）- 权重最高
        keyword_matched = set()
        matched_keywords = []
        for keyword, q_ids in keywords.items():
            if keyword in query_text:
                keyword_matched.update(q_ids)
                matched_keywords.append(keyword)
        
        # 2. 获取关键词匹配的QA详情
        keyword_results = []
        seen_q_ids = set()
        
        for q_id in keyword_matched:
            if q_id not in seen_q_ids:
                results = self.table.search().where(f"q_id = '{q_id}'").to_list()
                if results:
                    item = results[0]
                    keyword_results.append({
                        "q_id": item.get("q_id") if isinstance(item, dict) else item.q_id,
                        "question": item.get("question") if isinstance(item, dict) else item.question,
                        "answer": item.get("answer") if isinstance(item, dict) else item.answer,
                        "content": item.get("content") if isinstance(item, dict) else item.content,
                        "matched_by": f"keyword:{','.join(matched_keywords)}",
                        "priority": 1.0  # 关键词匹配权重最高
                    })
                    seen_q_ids.add(q_id)
        
        # 3. 语义检索作为补充（只取未被关键词匹配到的）
        semantic_results = self.query(query_text, top_k=top_k * 2)
        
        # 4. 合并结果：关键词匹配优先，语义检索补充
        final_results = keyword_results.copy()
        
        for item in semantic_results:
            if item["q_id"] not in seen_q_ids and len(final_results) < top_k:
                final_results.append({
                    "q_id": item["q_id"],
                    "question": item["question"],
                    "answer": item["answer"],
                    "content": item["content"],
                    "matched_by": "semantic",
                    "priority": 0.5  # 语义匹配权重次之
                })
                seen_q_ids.add(item["q_id"])
        
        logger.debug(f"🔍 检索结果: {len(keyword_results)}个关键词匹配, {len(final_results) - len(keyword_results)}个语义匹配")
        return final_results[:top_k]
    
    def get_all_documents(self) -> List[Dict[str, str]]:
        """获取所有文档"""
        results = self.table.search().to_list()
        return [
            {
                "q_id": item.get("q_id") if isinstance(item, dict) else item.q_id,
                "question": item.get("question") if isinstance(item, dict) else item.question,
                "answer": item.get("answer") if isinstance(item, dict) else item.answer,
                "content": item.get("content") if isinstance(item, dict) else item.content
            }
            for item in results
        ]
    
    def count_documents(self) -> int:
        """获取文档数量"""
        return self.table.count_rows()
    
    def clear_all(self) -> None:
        """清空所有文档"""
        self.table.delete(self.table.search())
        logger.info("🗑️ 知识库已清空")


# 预定义的关键词触发规则
PERSONA_KEYWORDS: Dict[str, List[str]] = {
    "名字": ["Q01"],
    "年龄": ["Q02"],
    "活动": ["Q03"],
    "来自": ["Q04"],
    "家人": ["Q05"],
    "宠物": ["Q06"],
    "猫": ["Q07"],
    "狗": ["Q07"],
    "动物": ["Q07"],
    "长相": ["Q08"],
    "发型": ["Q09"],
    "染发": ["Q10"],
    "耳洞": ["Q11"],
    "化妆": ["Q13"],
    "化妆品": ["Q14"],
    "美甲": ["Q15", "Q16"],
    "衣服": ["Q17"],
    "牌子": ["Q19", "Q20"],
    "性格": ["Q22"],
    "喜欢": ["Q26"],
    "歌": ["Q27", "Q45"],
    "癖好": ["Q29"],
    "交朋友": ["Q30"],
    "住": ["Q31"],
    "受不了": ["Q32"],
    "战士": ["Q33"],
    "魔法师": ["Q33"],
    "天使": ["Q34"],
    "恶魔": ["Q34"],
    "分身": ["Q36"],
    "回忆": ["Q38"],
    "歌曲": ["Q39"],
    "直播": ["Q46", "Q53", "Q77"],
    "户川纯": ["Q24", "Q27", "Q45", "Q47", "Q58", "Q59"],
    "BLAME": ["Q62"],
    "电影": ["Q63"],
    "学校": ["Q64", "Q65"],
    "体育": ["Q66"],
    "游戏": ["Q68"],
    "特长": ["Q70"],
    "颜值": ["Q71"],
    "起床": ["Q72"],
    "睡觉": ["Q73"],
    "学习": ["Q74"],
    "资格证": ["Q75"],
    "季节": ["Q80"],
    "节日": ["Q81"],
    "包包": ["Q82", "Q83"],
    "害怕": ["Q85"],
    "打工": ["Q87"],
    "恋爱": ["Q88"],
    "约会": ["Q89"],
    "表白": ["Q90"],
    "休息": ["Q91"],
    "未来": ["Q96"],
    "目标": ["Q97"],
    "可爱": ["Q98"],
    "生活": ["Q99"],
}

# 压力状态相关的知识库分类
STRESS_KEYWORDS: Dict[str, List[str]] = {
    "累": ["Q55", "Q76", "Q91", "Q100"],
    "烦": ["Q62", "Q92"],
    "闭嘴": ["Q62", "Q92"],
    "滚蛋": ["Q92"],
    "无聊": ["Q07", "Q20"],
}


# 创建全局知识库实例
knowledge_base = None

def init_knowledge_base() -> KnowledgeBase:
    """初始化知识库（单例）"""
    global knowledge_base
    if knowledge_base is None:
        knowledge_base = KnowledgeBase()
    return knowledge_base


def load_qa_pairs_from_text(qa_text: str) -> List[Tuple[str, str, str]]:
    """
    从文本中解析QA对（使用正则增强解析器）
    
    Args:
        qa_text: 包含QA对的文本，格式如 "Q01.问题\n答案\nQ02.问题\n答案..."
    
    Returns:
        QA三元组列表
    """
    qa_list = []
    
    # 使用正则表达式 ^Q\d+\. 来分割QA数据
    # 匹配以Q开头，后面跟数字，再跟点号的行
    pattern = re.compile(r'^Q\d+\.', re.MULTILINE)
    
    # 找到所有匹配的位置
    matches = list(pattern.finditer(qa_text))
    
    if not matches:
        logger.warning("⚠️ 未找到QA数据格式")
        return qa_list
    
    # 遍历每个匹配，提取QA对
    for i in range(len(matches)):
        start_match = matches[i]
        q_id = start_match.group(0)[:-1]  # 去掉末尾的点号
        
        # 确定当前QA的结束位置
        if i < len(matches) - 1:
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(qa_text)
        
        # 提取QA内容
        qa_content = qa_text[start_match.end():end_pos].strip()
        
        # 分割问题和答案（第一个换行符分隔）
        parts = qa_content.split('\n', 1)
        if len(parts) == 2:
            question = parts[0].strip()
            answer = parts[1].strip()
        else:
            question = qa_content.strip()
            answer = ""
        
        if question:  # 确保问题不为空
            qa_list.append((q_id, question, answer))
            logger.debug(f"解析QA: {q_id} - {question[:20]}...")
    
    logger.info(f"📝 成功解析 {len(qa_list)} 条QA数据")
    return qa_list